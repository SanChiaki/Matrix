// matrix 的 dsh 审批应答者：Agent 要在工作目录以外操作时（沙箱升权），把申请转给网络，由 Agent 的所有者决定。
//
// dsh-user-approval 把 `ask` 策略下的每个请求派发给 `approval/request` waterfall；没有应答者时请求以
// `unavailable` 拒绝。这个插件就是那个应答者：POST 到网络的 /internal/approval，阻塞到有结果。
// 身份由执行上下文令牌认定（只在这次运行期间有效），不信任模型自报。
// 只依赖 Node 标准库（全局 fetch），由 matrix 生成的 --patch 覆盖层按绝对路径挂载。

export const name = "matrix-approval";

const OUTCOMES = new Set(["allowed-once", "rejected", "cancelled", "unavailable"]);

export function apply(ctx, config) {
  ctx.on("approval/request", async (req) => {
    const controller = new AbortController();
    const onAbort = () => controller.abort();
    req.signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => controller.abort(), (config.timeoutMs ?? 900000) + 30000);
    try {
      const res = await fetch(config.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: config.token, tool: req.toolName ?? "", reason: req.reason ?? "" }),
        signal: controller.signal,
      });
      if (!res.ok) return "unavailable";
      const { outcome } = await res.json();
      return OUTCOMES.has(outcome) ? outcome : "unavailable";
    } catch {
      return req.signal?.aborted ? "cancelled" : "unavailable";
    } finally {
      clearTimeout(timer);
      req.signal?.removeEventListener("abort", onAbort);
    }
  });
}
