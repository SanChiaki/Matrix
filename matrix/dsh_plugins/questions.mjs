// matrix 的 dsh 提问应答者：Agent 调 ask_user_question 时，把问题转给网络，等人或委托方回答。
//
// dsh 的问题服务（dsh-user-questions）把每次提问派发给 `user-questions/request` waterfall；headless 里没有应答者时
// 以 NO_PROVIDER 失败（research: deepseek-harness#user-questions）。这个插件就是那个应答者：POST 到网络的
// /internal/question，阻塞到有回答。回答作为工具结果交回模型，不是一条消息，别人伪造不了。
// 等多久、问谁由网络决定；没得到回答时抛出错误，模型看到的是 `Error: <原因>`，不会当成同意。
// 身份由执行上下文令牌认定（只在这次运行期间有效），不信任模型自报。
// 只依赖 Node 标准库（全局 fetch），由 matrix 生成的 --patch 覆盖层按绝对路径挂载。

export const name = "matrix-questions";

function plain(q) {
  // 只转问题本身：id、问题、标题、补充说明、选项、是否多选（seam 里叫 multiSelect）
  return {
    id: String(q.id ?? ""),
    question: String(q.question ?? ""),
    header: q.header == null ? "" : String(q.header),
    detail: q.detail == null ? "" : String(q.detail),
    multi_select: Boolean(q.multiSelect ?? q.multi_select),
    options: (Array.isArray(q.options) ? q.options : []).map((o) => ({
      label: String(o?.label ?? ""),
      description: o?.description == null ? "" : String(o.description),
    })),
  };
}

export function apply(ctx, config) {
  ctx.on("user-questions/request", async (req) => {
    const controller = new AbortController();
    const onAbort = () => controller.abort();
    req.signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => controller.abort(), (config.timeoutMs ?? 3600000) + 30000);
    let res;
    try {
      res = await fetch(config.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: config.token, questions: (req.questions ?? []).map(plain) }),
        signal: controller.signal,
      });
    } catch (e) {
      if (req.signal?.aborted) throw e;
      throw new Error("没有得到回答：连不上网络");
    } finally {
      clearTimeout(timer);
      req.signal?.removeEventListener("abort", onAbort);
    }
    const body = res.ok ? await res.json().catch(() => ({})) : {};
    if (!Array.isArray(body.answers)) throw new Error(String(body.error || "没有得到回答"));
    return {
      answers: body.answers.map((a) => ({
        id: String(a?.id ?? ""),
        selected: Array.isArray(a?.selected) ? a.selected.map(String) : [],
        ...(a?.custom ? { custom: String(a.custom) } : {}),
      })),
    };
  });
}
