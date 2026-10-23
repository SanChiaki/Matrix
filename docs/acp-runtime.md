# 用 ACP 运行 Agent（设计草案）

状态：**草案，待定**。这份文档只做方案，不切换运行时；`DshRuntime`（`dsh headless --json`）仍是默认。
动手前要先过第 8 节的验证项。相关设计：[device-runtimes.md](device-runtimes.md)（运行时契约、能力报告、端侧驱动），
约束：[AGENTS.md](../AGENTS.md)、[invariants.md](reference/invariants.md)、[security.md](reference/security.md)。

依据的上游版本（2026-10-22 读源码与 README，只读、未运行）：

| 来源 | 版本 |
| --- | --- |
| deepseek-harness（MIT）`packages/acp/acp`、`packages/sdk/{protocol,server}`、`packages/bundle/{acp-app,headless}`、`packages/mcp/mcp-client` | `master@687f282` |
| `@agentclientprotocol/claude-agent-acp`（Apache-2.0，原 `@zed-industries/claude-code-acp`，旧包已弃用） | 0.94.0（2026-10-21） |
| `@agentclientprotocol/codex-acp`（Apache-2.0，原 `@zed-industries/codex-acp`，旧包已弃用） | 2.2.2（2026-10-21） |
| ACP SDK：`@agentclientprotocol/sdk` / PyPI `agent-client-protocol` | 2.0.0 / 0.12.1 |

---

## 0. 结论

1. **值得换，但先只换传输，不换安全机制。** dsh 的 ACP 服务（`dsh acp`，即 `dsh-acp-app` 这个 profile）
   能给 headless 给不了的东西：图片输入、显式取消（会话落盘后再结束，不是杀进程）、带原因的失败、
   同一个协议覆盖三家 harness。`build_patch_yaml` 生成的覆盖层（禁用插件、沙箱模式、审批应答）**原样保留**，
   ACP 只替换"怎么送提示词、怎么收事件、怎么停"。
2. **一轮一个进程**，和现在一样。dsh 的插件覆盖层与沙箱默认模式是进程级的，ACP 不提供按会话设置沙箱的入口
   （`modes` 不支持），执行上下文令牌又是按轮签发的；所以长驻进程拿不到任何安全上的好处，先不做。
3. **网络工具继续由覆盖层挂载，不走 ACP 的 `mcpServers`。** 经 ACP 声明的 MCP 服务器拿不到
   `toolCallTimeoutMs`，会落到 dsh-mcp-client 的默认 60 秒；网络工具一次调用最长要等 `tool_timeout_s`（默认 900 秒）。
4. **Claude Code 与 Codex 都有在维护的 ACP 适配器**（第 5 节已核实）。一个 ACP 客户端可以替代三个驱动里的
   **传输与事件解析**，但每家仍要一份"档案"（启动命令、环境、权限到会话参数的映射、能力声明）。
   而且适配器能把审批请求转出来，**有机会把 Claude Code / Codex 从 `approval: none` 提升到 `interactive`**，
   这是这条路最大的收益，也是最需要验证的地方。

---

## 1. 现状与动机

`DshRuntime.run` 每轮起一个 `dsh headless --patch <覆盖层> --json [--session-id S] -`：提示词从 stdin 进，
JSON 事件（`session` / `text` / `thinking` / `tool_call` / `tool_result` / `status` / `final`）从 stdout 出，
退出码与 stderr 判失败。端侧的 Claude Code（`claude -p --output-format stream-json`）与 Codex（`codex exec --json`）
各有一套参数和输出格式。

headless 的限制：

- **只收文本**：提示词是 stdin 上的一段字符串，图片到不了模型。
- **停只能杀进程**：超时与人终止都走 `watch_process` → `proc.kill()`；会话靠 dsh 下次续接时修复中断的最后一轮。
- **失败靠解析 stderr**：`dsh: <code>: <message>`，`classify_error` 按子串匹配。
- **三家三套解析**：`drivers/claude.py`、`drivers/codex.py` 各自猜事件字段，上游一改就坏（device-runtimes.md 第 13 节）。

ACP（Agent Client Protocol）是"宿主 ↔ 本地 Agent 进程"的标准协议，宿主掌权：启动进程、给工作目录、挂 MCP、
回答权限请求（参照 Kylon 的 BYOA 做法：ACP 管运行时，网络工具以 MCP 注入，research: kylon#byoa-acp，
research: acp-vs-a2a#acp-methods）。dsh 的另一套 SDK JSON-RPC（`dsh --profile sdk`）**不合适**：它没有取消与关闭会话的方法
（"a client abandons a turn by closing the runtime process"），`session/prompt` 只返回入队回执、没有按提示词的结果。

---

## 2. dsh-acp 已核实的能力

| 方法 / 能力 | dsh-acp 的行为 | 对 Matrix 的意义 |
| --- | --- | --- |
| `initialize` | 协议 v1；`sessionCapabilities: {close, list, resume}`；`mcpCapabilities.http: true`；`promptCapabilities.image` 只在附件存储就位且**该 profile 配置的模型路由**支持图片时为真 | 能力以握手为准，缺了就按失败处理（不降级到 headless 语义） |
| `session/new` | 绝对路径 `cwd`；stdio / HTTP MCP 在发布前验证，连不上整体回滚 | 席位 / 任务第一轮 |
| `session/resume` | 恢复已落盘、未在本进程活跃的根会话；`cwd` 必须是同一目录；不重放历史；**拒绝 subagent 会话** | 后续轮次（唤醒、返工、续接） |
| `session/load` | **不支持**（不重放历史） | 不需要：Matrix 自己有对话记录 |
| `session/prompt` | 文本、resource link、图片（PNG/JPEG/WebP/GIF）按顺序进一条用户消息；每会话同时只有一个提示词；等子 Agent 与输出都落定才返回 `stopReason` | 一轮 = 一次 `session/prompt` |
| `session/cancel` | 停掉当时的根活动并等它的输出排空；会话保留，之后可以再提示 | 超时、人终止 |
| `session/close` | 静默取消、排空更新、清理子 Agent、**刷盘**，只释放这个会话 | 每轮结束都关 |
| `session/update` | 已提交的助手消息与思考、通用工具生命周期（`tool_call` / `tool_call_update`）、配置变化、上下文用量 | 映射到我们的事件 |
| `session/request_permission` | 只给一次性选项 `allow-once` / `reject-once`；只带 `toolCallId`，不带原因 | 审批（见 3.4） |
| 不支持 | 删除、fork、附加目录、SSE MCP、modes、commands、plans、terminals、**客户端文件系统**、elicitation | 读隔离不能靠"文件访问经过客户端"来做 |

失败与停止（`session.ts`、`codec.ts`）：一轮内的 Agent 失败或 `turn/end` 带错误时，`session/prompt` 以 JSON-RPC
`internalError("turn failed: <错误链>")` 拒绝；`completed` → `end_turn`，`max-tokens` → `max_tokens`，
显式取消 → `cancelled`；被钩子中止、`blocked` 等普通结束也报 `end_turn`。

`dsh-acp-app` 和 headless 一样是叠在 `dsh-base` 上的 profile 补丁：工具、持久化、策略、凭据都来自 base，
所以 `--patch` 覆盖层的插件 id（`tool-bash`、`tool-web`、`tool-plugin-manager`、`dsh-user-approval` 等）应当照样生效
（第 8 节第 1 项验证）。它默认的模型路由写在 `acp` 这一行的 `config.provider/model` 里，不读 `agent-default-model`。

---

## 3. Matrix 的各项怎么映射

### 3.1 会话

- **一个席位或一个任务 = 一个 ACP 会话**，会话 id 仍存在 `Seat.dsh_session_id` / `Task.dsh_session_id`（不变量 7）。
- 第一轮 `session/new {cwd: 工作目录, mcpServers: []}`；之后每轮 `session/resume {sessionId, cwd}`。`cwd` 校验正好防止
  拿错目录续接；被拒（"not resumable" / "cwd does not match"）按 `runtime_error` 失败，**不悄悄开新会话**。
- 每轮结束 `session/close` 再关 stdin。同一进程里一个会话活跃时 `resume` 会被拒（"already active"），一轮一进程不会碰到。
- 旧会话：headless 与 acp 共用 `dsh-base` 的会话持久化，headless 建的根会话**应当**能被 `session/resume` 接上；
  这是迁移能否无感的关键，第 8 节第 2 项验证。接不上时，已有席位继续用 headless，新席位用 ACP（席位记一下运行时）。

### 3.2 事件

运行时内部把 ACP 更新翻译成现有事件，`_accumulate`、`on_event`、引擎、前端都不用改：

| ACP | 现有事件 |
| --- | --- |
| `session/new` / `resume` 成功 | `{"type": "session", "sessionId"}`（由运行时合成） |
| `agent_message_chunk`（按 `messageId` 拼接，消息结束时发） | `{"type": "text", "text"}`；最后一条助手消息作为 `final_text` |
| `agent_thought_chunk` | `{"type": "thinking", "text"}` |
| `tool_call` | `{"type": "tool_call", "callId": toolCallId, "tool": title, "input": rawInput}` |
| `tool_call_update`（`completed` / `failed`） | `{"type": "tool_result", "callId", "status", "result": content 拼成文本}` |
| `usage_update`（`used` / `size`） | `{"type": "status", "context": {...}}`；**没有累计 token**，`RunResult.usage` 会变空（目前引擎不用它） |
| `session/prompt` 返回 | `{"type": "final", "text"}` |

dsh 的 `tool_call.title` 就是工具名（`bash`、`mcp__matrix_net__delegate`……），所以 `_violations` 按 `TOOL_AUTHORITY`
检查越权（不变量 11）照旧成立。Claude / Codex 的适配器里 `title` 是给人看的标题，工具名要从各家字段取（第 5 节）。
原始 ACP 帧照旧追加到工作目录的事件日志（`dsh-events.jsonl` 改名 `acp-events.jsonl`）。

### 3.3 网络工具

保持现状：覆盖层 `insert` 一个 `dsh-mcp-client`，stdio 启动 `matrix/mcp_server.py`，环境里带
`MATRIX_TOOL_URL`、`MATRIX_TOKEN`（本轮的执行上下文令牌，不变量 10）、`MATRIX_TOOLS`，
`toolCallTimeoutMs = tool_timeout_s + 60s`。`session/new` 的 `mcpServers` 传空。

不改用 ACP `mcpServers` 的原因：

- dsh-acp 把声明翻成 `McpClient.Config` 时不传 `toolCallTimeoutMs`，默认 60 秒；`delegate` 等工具会等审批、等人，会被误判超时。
- HTTP 形态能省掉一个子进程，但 `/internal/tools` 不是 MCP 端点（它收 `mcp_server.py` 转来的 `{token, tool, args}`），
  要另开一个 Streamable HTTP MCP 端点；令牌还会作为请求头出现在 ACP 帧里。等上游支持超时、且确认 harness 日志不落请求头之后再考虑。

要验证的一点：覆盖层在根上挂的 MCP 工具，ACP 新建的会话 Agent 看不看得见（第 8 节第 1 项）。看不见就只能走 `mcpServers`，
那时 60 秒超时是硬阻碍，需要先给上游提改动。

### 3.4 权限、沙箱、审批（不能被削弱）

- **插件禁用与沙箱不变**：`build_patch_yaml` 照旧生成覆盖层（`AUTHORITY_PLUGINS` 按权限禁用、`tool-plugin-manager` 始终禁用），
  `DSH_PERMISSION_MODE` 照旧按 `file.write` 设 `workspace-write` / `read-only`。ACP 的 `session/new` 里没有任何能放宽它们的参数
  （不支持 modes），客户端也不发 `session/set_config_option` 以外的配置。
- **能力声明不变**：`capabilities()` 仍是 `all_enforced("dsh", approval="interactive", sandbox="workspace-write")`，
  前提是第 8 节第 1 项证明覆盖层在 ACP profile 下同样生效；证明之前不切换。
- **审批**：dsh-acp 自己也在 `approval/request` 上注册了应答者，会把升权申请转成 `session/request_permission` 发给客户端。
  方案 A（倾向）：ACP 模式下**不挂** `approval.mjs`，客户端收到 `request_permission` 后在进程内调
  `ApprovalBridge.handler(tool, reason)`，`tool` 取同一 `toolCallId` 的 `tool_call.title`，`reason` 取它的 `rawInput`；
  dsh 先排空更新再发请求，所以那条 `tool_call` 一定先到。方案 B：保留 `approval.mjs`（它能拿到 dsh 的 `reason` 字段），
  并验证它在 waterfall 里先于 ACP 应答者生效。选哪个取决于 `rawInput` 里有没有升权理由（第 8 节第 3 项）。
  两个方案都要满足：**每次升权恰好一条 Approval 记录**；客户端**只选 `allow_once` / `reject_once`**，永远不选 `allow_always` 一类
  （不变量 14：获批只对那一次调用生效）；没有 handler、handler 出错、超时一律 `reject_once` 或 `cancelled`，**绝不自动批准**。

### 3.5 模型

覆盖层不再写 `agent-default-model`，改写 `acp` 这一行的 `config.provider/model`（"a later patch can replace that row's complete config"）。
这样 `initialize` 报告的图片能力就是这个 Agent 实际用的模型的能力。`reasoning_effort` 在会话建立后用
`session/set_config_option` 设（dsh 只在该模型声明了这个选项时才提供）。`DshRuntime.default_model()` 改读 `acp` 行。

### 3.6 超时

`RunRequest.timeout_s` 的语义不变（None = 不限制）。`watch_process` 换成"先礼后兵"：到点发 `session/cancel`，
等 `session/prompt` 以 `cancelled` 返回（宽限 10 秒，参照 Kylon 取消后 5 秒内落定的约定，research: acp-vs-a2a#acp-methods），
再 `session/close`；宽限内没落定才杀进程。结果仍是 `TIMEOUT`（可续接一次）。和现在比，会话是刷过盘的，续接更可靠。

### 3.7 终止（人点停止）

`RunRequest.cancel` 置位 → 同一条路径发 `session/cancel` → `STOPPED`（不自动续接）。正在等待的 `request_permission`
立即回 `cancelled`。注意引擎里那条 Approval 记录此时还挂着，要有一个"这轮结束，作废它的待审批申请"的入口
（重启时已有同样的作废逻辑，见不变量 12），否则所有者会批一个已经不存在的请求。

### 3.8 图片

- `RunRequest` 增加 `images: list[Path]`（工作目录里的文件，来自聊天附件或项目档案）；运行时在
  `promptCapabilities.image` 为真时把它们按顺序作为 `{"type": "image", "mimeType", "data": base64}` 放在文本后面。
- 不支持时（模型路由不支持、或 headless）**不报错**：提示词里写明"附件 x.png 在工作目录，当前模型看不了图片"，
  模型仍可以用工具处理文件。
- 端侧：`run.request` 加 `images`（引用随 `materials` 下发的文件），由连接器读取后发送；图片不经过 ACP 以外的通道。
- 大小与格式由运行时预先检查（dsh 只收 PNG/JPEG/WebP/GIF），超限按上一条降级，不让一张坏图让整轮失败。

### 3.9 失败分类

`session/prompt` 的 JSON-RPC 错误文本进 `RunResult.error`，`classify_error` 照旧按 `MALFORMED_RESPONSE`、`RATE_LIMIT`
等代码匹配；需要验证错误链里确实带这些代码（第 8 节第 4 项），不带就在运行时里补一层映射。进程非零退出、握手失败、
`resume` 被拒都按 `runtime_error`。`stopReason: max_tokens` 按 `EMPTY_RESPONSE` 一类处理（可续接一次）。

---

## 4. 进程模型与重启

**一轮一个进程**：`dsh acp --patch <本轮覆盖层>`，`initialize` → `new` / `resume` →（可选）`set_config_option` →
`prompt` → `close` → 关 stdin，进程按 EOF 有界退出。

为什么不按 Agent 长驻：

| 进程级的东西 | 为什么不能共用 |
| --- | --- |
| 插件覆盖层、`DSH_PERMISSION_MODE` | 同一 Agent 在不同委托里的有效权限不同（不变量 1、3），按会话放不进去 |
| 网络工具的令牌 | 按轮签发、只在这轮有效（不变量 10）；挂在覆盖层里就绑定进程 |
| `acp` 行的模型 | 决定 `initialize` 报告的图片能力 |

长驻只省掉 dsh 的启动时间，而现在每轮本来就要启动一次，没有退步。以后真要省启动开销，可以做一个按
`(Agent, 有效权限, 模型)` 为键的进程池，令牌改走 `mcpServers`（每轮 `resume` 时重新声明）——前提是 3.3 的超时问题已在上游解决。

**重启语义（不变量 12）不变**：ACP 进程是服务（或连接器）的子进程，父进程退出时 stdin 断开，dsh 按 EOF 收尾并刷盘。
云侧运行中的任务照旧判 `FAILED`、可续接，续接时 `session/resume` 接上同一会话；端侧任务照旧回 `WAITING`，
连接器重启后由网络重新派发，驱动同样 `resume`。待审批的外溢申请随那次运行作废。

---

## 5. Claude Code 与 Codex 的 ACP 适配器

### 5.1 核实结果

两家都有**在维护**的适配器，现归 `agentclientprotocol` 组织，2026-10-21 都发过版：

| | `claude-agent-acp` 0.94.0 | `codex-acp` 2.2.2 |
| --- | --- | --- |
| 实现 | 基于 Claude Agent SDK，`pathToClaudeCodeExecutable` 可由 `CLAUDE_CODE_EXECUTABLE` 指向本机 `claude` | 启动 Codex App Server 并转译；npm 包自带一个 `@openai/codex`，`CODEX_PATH` 可改用本机的 |
| 协议 | v1；另有草案 v2（`CLAUDE_AGENT_ACP_PROTOCOL=v1` 可钉死） | v1 |
| 会话 | `loadSession: true`，`resume`、`close`、`list` | `loadSession: true`，`resume` |
| 图片 | 支持 | 支持 |
| MCP | 客户端 stdio / HTTP | 客户端 stdio / HTTP |
| 权限请求 | SDK 的 `canUseTool` → `session/request_permission` | `on-request` 审批、`reviewer: user` 时 → `session/request_permission` |
| 工具开关 | `_meta.claudeCode.options` 透传 SDK 选项：`tools`（内置工具清单，硬开关）、`allowedTools` / `disallowedTools`、`sandbox`、`settings`、`settingSources` | 模式：`read-only`、`workspace-write`（都 `networkAccess: false`、`on-request`），另有 `agent`（自动审查，**默认**）与 `agent-full-access`；`INITIAL_AGENT_MODE`、`CODEX_CONFIG` |
| 工具名 | `tool_call._meta.claudeCode.toolName` | 按 `kind` 与 `rawInput` 区分命令执行、文件改动、MCP 调用 |

研究笔记里"npm scope 待验证"一条可以结了：`@zed-industries/*` 已弃用，指向 `@agentclientprotocol/*`
（research: local-harness-integration#acp-platforms）。

### 5.2 一个 ACP 驱动能不能替代三个

**能替代的是传输层，不是档案层。** 拆成：

- `AcpClient`（共用）：stdio 上的 JSON-RPC、握手与能力检查、`new` / `resume` / `prompt` / `cancel` / `close`、
  更新到事件的翻译、`request_permission` 转审批、超时与终止。云侧 `AcpRuntime` 与端侧驱动都用它。
- `AcpProfile`（每家一份，几十行）：启动命令与环境、权限到会话参数的映射、工具名的取法、能力声明。

| | dsh | Claude Code | Codex |
| --- | --- | --- | --- |
| 启动 | `dsh acp --patch <覆盖层>` | `claude-agent-acp`；环境先 `clean_env()`（去掉宿主凭据变量，见 `drivers/claude.py`） | `codex-acp`，`INITIAL_AGENT_MODE` 按 `file.write` 取 `workspace-write` / `read-only`，**不能用默认的 `agent`** |
| `web.research` / `shell.exec` / `file.write` | 覆盖层禁插件 + 沙箱 | `_meta.claudeCode.options.tools` 只列授予的工具（比 `allowedTools` 硬：不在清单里就不存在）；`settingSources` 收紧，避免用户自己的 `permissions.allow` 规则在 `canUseTool` 之前放行；`allowDangerouslySkipPermissions: false` | 沙箱模式；`web.research` 仍按 `advisory`（联网开关不在模式里） |
| 审批 | 见 3.4 | 有望 `interactive`（待验证） | 有望 `interactive`（待验证） |
| 网络工具 | 覆盖层挂载 | `mcpServers`（Claude Code 的 MCP 超时另设，待验证） | `mcpServers`（`_meta.mcpStartupAwaitTimeoutMs` 等启动完成） |

在第 8 节的一致性检查通过之前，**原生驱动保留**；连接器对同一个 harness 只启用一种驱动，报告的能力按实际启用的那种算
（device-runtimes.md 2.3 节"不变量能不能守住"的标准不变）。ACP 路线的已知陷阱——别人的做法是默认 `allow`、不等审批
（research: acp-vs-a2a#acp-security-trap，research: xmatrix#runtime-enforcement）——不照搬：Matrix 的客户端
永远把请求转给所有者，做不到强制的权限照旧记 `advisory`。

---

## 6. 差距

**headless 有、ACP 没有（或要绕）：**

| 差距 | 处理 |
| --- | --- |
| ACP 声明的 MCP 没有调用超时设置（默认 60 秒） | 网络工具继续走覆盖层（3.3） |
| 沙箱模式、插件禁用不能按会话设 | 一轮一进程（第 4 节） |
| `request_permission` 不带升权理由 | 从 `rawInput` 取或保留 `approval.mjs`（3.4） |
| 没有累计 token 用量，只有上下文占用 | 引擎目前不用 `usage`；需要时向上游提 |
| 失败只有一段错误文本，`blocked` 等结束也报 `end_turn` | 运行时补映射（3.9） |
| headless 续接会检查 agent preset；ACP 只查 `cwd` 与是否子会话 | Matrix 不用 preset，影响不大 |

**ACP 有、headless 没有：**

| 能力 | 用处 |
| --- | --- |
| 图片输入 | 聊天里的截图、图表能给模型看（3.8） |
| `session/cancel`（会话刷盘后结束） | 超时与终止后续接更稳（3.6、3.7） |
| 结构化的权限请求 | 统一三家的审批上行 |
| `session/list` | 排查与修复丢失的会话 id |
| 一个连接多会话、`set_config_option` 换模型 | 将来的进程池 |
| 三家同一协议 | 少维护两套输出解析 |

两边都没有：运行中追加输入（steer）。dsh-acp 每会话一次一个提示词；Claude 适配器的 README 提到 v1 下有 steering，具体形式未核实。
`RuntimeCapabilities.steer` 继续是 `False`，运行中到达的消息照旧排队。读隔离两边也都没有：dsh-acp 不支持客户端文件系统，
需求 #34 的风险不因换协议而改变。

---

## 7. 分阶段计划

`ScriptedRuntime` 始终是确定性测试运行时，引擎测试不依赖任何 ACP 进程。ACP 相关测试用一个**假 ACP Agent**：
`tests/fake_acp.py`，仅标准库，按环境变量里的脚本回放握手、更新、权限请求、错误，并响应取消（和现有 `fake_bin` 思路一致）。

| 阶段 | 内容 | 测试 |
| --- | --- | --- |
| 0. 验证 | 跑第 8 节各项，结论写回本文档；任何一项不过就停在这里 | 手工 + 记录 |
| 1. `AcpClient` | `matrix/acp.py`：仅标准库的行分隔 JSON-RPC 客户端（同 `mcp_server.py` 的取舍，不引入依赖），握手检查、会话方法、更新翻译、超时与取消 | 假 Agent：事件映射、`final_text`、握手缺能力即失败、取消返回 `STOPPED`、超时返回 `TIMEOUT` 且会话 id 保留、错误文本分类、`request_permission` 只选一次性选项且无 handler 时拒绝 |
| 2. 云侧 `AcpDshRuntime` | 与 `DshRuntime` 并存，`--runtime dsh-acp` 选择；覆盖层改写 `acp` 行；默认仍是 headless | `build_patch_yaml` 在 ACP 模式下仍禁用同样的插件（扩展 `test_dsh_patch_reflects_authority_and_mounts_mcp`）；审批恰好一条记录（不变量 14）；终止时待审批申请作废；重启后 `resume` 续接（不变量 12）；`test_authorities_registered_everywhere` 不变 |
| 3. 图片 | `RunRequest.images`、`run.request.images`、降级提示 | 支持 / 不支持图片两种握手下的提示词内容；超限降级；端侧材料引用 |
| 4. 端侧 ACP 驱动 | `drivers/acp.py` + 三份 `AcpProfile`；连接器检测适配器是否安装；原生驱动保留 | 每家档案的命令与会话参数（类似现有 `test_claude_command_maps_authority_to_tool_whitelist`）；Codex 不能落在 `agent` 模式；Claude 的 `tools` 清单与权限一致；`clean_env` 仍生效 |
| 5. 一致性检查与切换 | 对真实 harness 跑 device-runtimes.md 第 10 节五个问题，声明 `enforced` / `interactive` 而拦不住的降级；全部通过后再改默认 | 真实环境清单（不进 CI） |

每个阶段都单独可合并、可回退；第 2 阶段之前任何东西都不影响现有运行路径。

---

## 8. 待验证（第 0 阶段）

1. **`dsh acp --patch <覆盖层>` 的组合**：禁用的插件（`tool-bash`、`tool-web`、`tool-plugin-manager`）在 ACP 会话里确实不可用；
   `DSH_PERMISSION_MODE=read-only` 下写入被拒；根上 `insert` 的 MCP 工具对 ACP 新建的会话可见。
2. **headless 会话能否被 `session/resume` 接上**（同一 `cwd`），以及反过来。
3. **升权理由在哪**：沙箱拒绝后模型申请升权时，`tool_call.rawInput` 里有没有理由；`approval.mjs` 与 dsh-acp 应答者同时挂载时谁先答。
4. **错误文本**：`MALFORMED_RESPONSE`、`RATE_LIMIT`、`TRANSPORT` 等代码是否出现在 `turn failed:` 的错误链里。
5. **取消的落定时间**：一个正在跑 `bash` 的轮次，`session/cancel` 后多久返回，子进程是否被清理。
6. **图片**：配置了支持图片的模型时 `promptCapabilities.image` 为真，模型确实看到了图。
7. **Claude 适配器**：`tools` 清单之外的工具确实不可调用；用户 `~/.claude/settings.json` 的 allow 规则在收紧 `settingSources` 后不再放行；
   工作目录以外的写会变成 `request_permission`；网络工具（`mcp__matrix_net__*`）不触发审批。
8. **Codex 适配器**：`INITIAL_AGENT_MODE=read-only` / `workspace-write` 生效、工作目录以外的命令变成 `request_permission`；
   `web_search` 是否受模式约束。

---

## 9. 明确不做的

- 不在这份草案里切换默认运行时，也不删除 headless 路径。
- 不经 ACP 放宽任何权限：不发 `set_mode` 一类请求，不接受适配器提供的"以后都允许"选项。
- 不把网络工具的令牌放进 ACP 帧或 HTTP 头，直到确认它们不会落进 harness 的会话日志。
- 不为 ACP 引入新的协作通道：Agent 之间的协作仍然只走网络工具（不变量 6）。
