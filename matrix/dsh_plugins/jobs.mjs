// matrix 的后台任务守护：Agent 要结束这一轮时还有自己起的后台任务在跑，就提醒它先收完（research: deepseek-harness#headless-tools）。
//
// dsh headless 在 Agent 空闲、没有子 Agent 时就退出，退出时后台任务（bash 的 run_in_background，或超时后自动转后台的命令）
// 一并被结束，完成通知也就送不到；dsh 的系统提示词却告诉模型“完成时会通知你，不要轮询”。这个插件在 Agent 转为空闲、
// 名下还有在跑的后台任务时，追加一条消息让它接着做：要结果就 job_output wait:true 等它收完，不要就 job_kill。
// 追加消息会让 Agent 重新忙起来，headless 的收尾检查看到会话变了就继续等。每次运行最多提醒 maxNudges 次，之后照常结束。
// 只依赖 Node 标准库，由 matrix 生成的 --patch 覆盖层按绝对路径挂载。

import { randomUUID } from "node:crypto";

export const name = "matrix-jobs";

function running(jobs, owner) {
  try {
    return jobs.list(owner).filter((j) => j.owner === owner && (j.status === "running" || j.status === "stopping"));
  } catch {
    return [];
  }
}

function notice(list) {
  const lines = list.map((j) => `- ${j.id}（${j.kind}）${j.label ? `：${j.label}` : ""}${j.progress ? `，进度 ${j.progress}` : ""}`);
  return [
    "<background_jobs>",
    "你还有后台任务在跑：",
    ...lines,
    "",
    "这一轮结束时它们会被终止，完成通知也收不到。要用它们的结果，就用 job_output（wait: true）等它们收完再继续；",
    "已经不需要的用 job_kill 结束。都处理完再给最终回复。",
    "</background_jobs>",
  ].join("\n");
}

export function apply(ctx, config) {
  const max = Number.isInteger(config?.maxNudges) ? config.maxNudges : 3;
  const spent = new Map();
  ctx.on("agent/status", ({ agent, status }) => {
    if (status !== "idle") return;
    const jobs = ctx.get("jobs");
    if (!jobs) return;
    const list = running(jobs, agent.id);
    if (list.length === 0) return;
    const n = spent.get(agent.id) ?? 0;
    if (n >= max) return;
    spent.set(agent.id, n + 1);
    try {
      agent.followup(Object.freeze({
        id: randomUUID(),
        role: "user",
        content: [{ type: "text", text: notice(list) }],
        source: { kind: "tool-jobs", form: "notice", summary: `${list.length} 个后台任务还在跑` },
      }));
    } catch (e) {
      ctx.logger?.warn?.(`matrix-jobs: 没能提醒 ${agent.id} 收后台任务：${e}`);
    }
  });
}
