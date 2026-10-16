# AGENTS.md

给在本仓库工作的 AI Agent 的项目说明与工程约束。动手前先读完本文件。

项目方向与设计理念见 [CHARTER.md](CHARTER.md)（行动纲领原文）。本文件讲"当前代码怎么做"，
CHARTER 讲"我们要往哪走"。需要判断某个改动是否该做时，以 CHARTER 的方向为准。

## 项目描述

**Matrix**（代码包名 `agentnet`）：一个本地运行的 Agent Network（Agent 协作网络）Demo。人在群聊里 @ Agent 交代任务（或和单个 Agent 私聊）；被 @ 的 Agent 自己做，或通过网络工具（MCP）把子任务委托给同组成员，网络负责授权、隔离、追踪与知识沉淀。

- 语言 / 依赖：Python ≥ 3.14，FastAPI + uvicorn，包管理用 `uv`，测试用 pytest。
- 运行时：默认以 DeepSeek Harness（DSH）headless 为 Agent 运行时；`scripted` 运行时是确定性脚本，用于测试与离线演练。
- 状态：全部持久化在 `--data` 目录（默认 `.agentnet/`，已被 gitignore）的 `state.json`；对话、任务各有独立工作目录 `conversations/<id>/`、`tasks/<id>/`；每个 Agent 有持久私有目录 `agents/<id>/`。

### 领域模型（`agentnet/models.py`）

| 概念 | 含义 |
| --- | --- |
| User | 邮箱 + 密码（scrypt）。第一个注册的用户接管已有的无主数据 |
| Agent（节点） | 名称、描述、能力、**自身权限**（运行时上限）、是否允许私聊、可选的运行时模型（provider / model / reasoning_effort，空则用运行时默认）；**所有者**与可见性（private / public / shared + `shared_with`）。系统不管理注入 Agent 上下文的经验 |
| Group（群组） | 成员集合，**没有主 Agent**，只对所有者可见。委托只能发给同组成员；网络**不会**自己创建 Agent，只接受 Agent 的提议、由人批准 |
| Conversation | 参照 Raft 的频道与 DM：`group`（群聊）**每个群组一个**，建群组即有，人在消息里 **@ 谁，谁接任务**；`dm`（私聊）**每个（用户, Agent）一个**，第一次打开时创建，不用 @，不能委托。群组 / Agent 删除后对应对话归档为只读 |
| Approval | Agent 想在工作目录以外操作时向**它的所有者**发起的一次性申请（pending / allowed / rejected / cancelled） |
| Seat（席位） | 一个 Agent 在一个对话里的执行上下文，id 为 `对话/Agent`：独立 DSH 会话、收件箱、工作目录 `conversations/<conv>/<agent>/`。第一次被 @ 时创建 |
| Task（委托） | Agent ↔ Agent 的一次授权：mission / authority / constraints / expected_outcome / share；带收件箱 |
| KnowledgeItem | 任务结果沉淀为知识，key 形如 `<task_id>:result` |
| Proposal | Agent 提议新建的 Agent，等人审核；`max_authority` 记录提议者当时的有效权限 |

### 群聊：被 @ 的成员接任务

参照 Raft（botiverse/raft）的频道模型：

- 人发的消息里 @ 到的群组成员（`@名称`，长名字优先匹配；或 API 的 `mentions`）被唤醒，在自己的席位上处理。它自己做，或在群组里找合适的成员委托。一条消息可以 @ 多个成员，各自的席位并行处理。
- 没 @ 任何人的消息留在群聊里，不唤醒任何人；只能 @ 群组成员。
- 席位被唤醒时收到**上次运行之后群聊里的新公开消息**（人的消息、其他成员的回复），其中 @ 它的那条标为“@ 你”。委托的过程、结果、知识不在公开消息里，只送给委托方（不变量 9）。
- 委托回报回到委托方的席位（或上一级任务），由它验收。
- 群成员随时可以加减（`POST /api/groups/<id>/members`、`DELETE …/members/<agent>`），变动作为 `event` 消息写进群聊，
  之后被唤醒的成员也能看到。移出群组不打断已经接下的任务，只是不能再被 @、被委托。
- 群聊、私聊不能删除，只能清空记录（`/clear`，席位会话从头开始，委托与知识不受影响）；只有归档的对话可以删除。
- 旧版对话（一个“主 Agent”）加载时迁移为只有这一个席位的群聊（原来在群组里）或私聊，沿用原会话与目录。
  旧版同一个群组 / Agent 有多个对话时，最近活跃的那个成为正式群聊 / 私聊，其余归档为只读（`Store._one_chat_each`）。

### 执行模型：异步委托 + 收件箱唤醒

参照 Raft 的 inbox + wake 模型：

- `delegate` **立即返回**任务 id，不阻塞。每个执行上下文（席位、任务）有一个收件箱；收件箱有内容且没在运行时，网络续接它的会话跑一轮。同一上下文同时只跑一轮，运行中到达的内容等这一轮结束再送达。
- 任务状态：`running → in_review → completed`，另有 `failed`（可续接）、`rejected`（授权阶段被拒）。交付进入 `in_review`，交付通知送进委托方收件箱；委托方 `accept_task` 或 `request_changes`（执行者在原会话返工）。送达那一轮结束仍未处理的交付视为接受（`auto_accepted`）。
- 执行者自己还有进行中的子任务或待审核提议时，这一轮的回复只记为进展（`task.notes`），等回报唤醒后再交付。
- 对话状态：`thinking`（有席位在跑一轮）/ `waiting`（有进行中的委托）/ `idle`。
- 失败语义：运行时错误按类别处理（`runtime.classify_error`）；`MALFORMED_RESPONSE`、`EMPTY_RESPONSE` 用同一会话自动续接一次。仍失败时这个**席位**进入 `blocked`（其他席位不受影响），失败消息列出它已完成的委托与已入库知识；人点“继续”或再 @ 它，同一会话从中断处接着做。失败的任务由委托方 `retry_task` 或人在 UI 上续接。

权限词表：`web.research`、`file.write`、`shell.exec`、`delegate`。

### 模块地图

| 文件 | 职责 |
| --- | --- |
| `agentnet/engine.py` | `Network` 核心：Agent/群组/对话管理、@ 解析、收件箱调度、席位与任务轮次、委托授权、验收与续接、提议审核、私有目录挂载、知识可见性、违规检测 |
| `agentnet/protocol.py` | 对话 / 任务 / 通知提示词、交付与失败说明、`TOOL_GUIDE`（网络工具用法） |
| `agentnet/runtime.py` | `AgentRuntime` 抽象；`DshRuntime`（`--patch` 覆盖层按权限禁用插件 + 沙箱 + 挂载 MCP + 按 Agent 设模型）；`ScriptedRuntime`；运行时错误分类 |
| `agentnet/mcp_server.py` | 仅标准库的 stdio MCP 服务，`TOOLS` 是网络工具 schema 的唯一定义，回调 `/internal/tools` |
| `agentnet/server.py` | FastAPI：登录、REST（按用户过滤）、看板 `/api/board`、SSE 事件流 `/api/events`、静态页面、运行时回调 `/internal/tools` 与 `/internal/approval` |
| `agentnet/auth.py` | 用户注册 / 登录 / 退出（scrypt，仅标准库）；第一个注册的用户接管已有数据 |
| `agentnet/dsh_plugins/approval.mjs` | dsh 审批应答者：沙箱升权申请转给网络，由 Agent 所有者决定 |
| `agentnet/store.py` | 持久化与种子数据（5 个种子 Agent + 「AI 产品机会研究组」；服务默认不预置，CLI 与测试预置） |
| `agentnet/events.py` | 进程内事件总线（带 seq，供 SSE 与 CLI 消费） |
| `agentnet/scripted.py` | 脚本运行时的场景处理器 |
| `agentnet/cli.py` | 无 UI 跑一轮对话并打印事件流 |
| `agentnet/static/` | 单页前端（原生 JS） |
| `tests/` | `test_network.py`（引擎不变量）、`test_api.py`（HTTP + MCP 回调）、`test_users.py`（登录、可见性、外溢审批、台账、看板） |
| `REQUIREMENTS.md` | 全部需求、实现状态与核心思路 |

### 常用命令

```bash
uv sync                                         # 安装依赖
uv run pytest -q                                # 全部测试（离线，不需要 dsh）
uv run agentnet --runtime scripted              # 启动服务 http://127.0.0.1:8765（离线）
uv run agentnet                                 # 使用 DSH 运行时（需 PATH 中有 dsh）
uv run python -m agentnet.cli "我要研究一个新的 AI 产品机会。" --runtime scripted   # 在群聊里 @ Product Strategy Agent
```

## 工程约束

### 一、网络不变量（改代码时必须保持，均有测试覆盖）

1. **权限只收窄不放大**：执行者有效权限 = 执行者自身权限 ∩ 委托授予；授予只能来自委托方的有效权限。
2. **只委托群组成员**：目标不在群组即拒绝（`REJECTED`）。网络不自动创建 Agent（CHARTER 第六节第 4 步
   "寻找或创建相关 Agent" 以"Agent 起草、人提交"的方式落地）：Agent 用 `propose_agent` 提议，提议的权限不能超出
   提议者当时的有效权限；人审核（可修改，权限仍受同一上限约束）后以人的身份创建，加入提议发生的群组，长期存在，
   人可删除。提议结果作为通知唤醒提议者。不要让 Agent 绕过人直接创建 Agent。
3. **约束沿委托链累加**，子任务继承全部上游约束。
4. **防环与限深**：目标已在委托链上则拒绝；深度达到 `max_depth`（默认 3）时移除 `delegate`、不挂载网络工具；
   每次被 @ 引出的工作（席位）/ 每个任务最多 `max_delegations`（默认 8）次委托，跨轮次累计；不能委托给自己。
   每个任务最多退回 `max_revisions`、续接 `max_retries` 次（默认各 3）。
5. **只有直接委托方能验收**：`accept_task` / `request_changes` / `retry_task` 只对自己在当前上下文发起的任务有效。
6. **协作只走网络**：系统中可见的 Agent 之间的协作必须经过网络工具（`delegate` / `share_knowledge` 等）。
   Agent 在自身运行时内使用 subagent / workflow / goal 等属于它自己的作用域，网络不加控制也不记违规；
   子 Agent 共用父 Agent 的插件覆盖层与沙箱，权限不会超出父 Agent。只有 `tool-plugin-manager`
   （可在运行期重新启用插件、绕过权限覆盖层）始终禁用。
7. **上下文默认隔离**：每个席位、每个委托任务独立会话、独立工作目录；后续轮次（唤醒、返工、续接）续接各自的会话。
   群聊里成员之间只共享公开消息，委托内部的内容不进群聊。
8. **私有目录归 Agent 自己**：`agents/<id>/` 每次运行挂载到工作目录的 `agent-home/`（运行前拷入、运行后同步回去），
   网络不读取、不注入提示词；没有 `file.write` 时只读。
9. **知识只显式共享**：结果只对产生者与直接委托方可见，其余必须通过 `share` 或 `share_knowledge`，且只能共享自己可见的知识。
10. **身份由网络认定**：调用方由运行期间才有效的执行上下文令牌识别，不信任模型自报身份。
11. **越权必留痕**：工具调用与权限不符时写入 `task.violations` 并发出 `authority.violation` 事件。
12. **重启语义**：重启后对话回到 `idle`（席位收件箱保留）；运行中的任务标记为 `FAILED`，失败通知进委托方席位或上级任务的收件箱，可续接；
    待审批的外溢申请作废（那次运行已随重启结束）。
13. **归属与可见性**：用户只看得到自己的群组 / 群聊 / 私聊、自己的与对自己开放（public / shared）的 Agent；委托、提议、知识、看板
    随对话归属。只有所有者能改 Agent。无权限一律按“不存在”（404）处理。SSE 推送前按用户过滤（`Network.event_visible`）。
    引擎方法的 `actor=None` 表示系统内部或测试，不做检查；HTTP 层必须传入当前用户。
14. **工作区外溢由所有者批准**：dsh 沙箱拒绝工作目录以外的写之后，模型可以申请升权重试一次；申请经 `dsh_plugins/approval.mjs`
    转到 `/internal/approval`，交给 **Agent 的所有者**（不是发起对话的人）。获批只对那一次调用生效；超时（`approval_timeout_s`）作废。
15. **并发不抢占**：同一 Agent 可同时为多人、多个任务工作。每轮提示词带工作台账（`Network.ledger`）；私有目录写回时按文件三方合并，
    期间被别的运行改过的文件不覆盖，另存 `*.conflict-*` 副本。

### 二、工作方式

1. 先读后改：改引擎前读 `engine.py` 与相关测试；保持现有风格（dataclass、类型标注、中文 docstring 与提示词、小工具函数）。
2. 新行为或修 bug 先补测试，用 `ScriptedRuntime` 写确定性测试，不依赖真实 DSH 或网络。
3. 每次改动后运行 `uv run pytest -q`，全部通过再交付。
4. 新增权限时同步改三处：`models.AUTHORITIES`、`models.TOOL_AUTHORITY`、`runtime.AUTHORITY_PLUGINS`；
   `test_authorities_registered_everywhere` 会在漏改时变红。
5. 新增网络工具时同步改：`mcp_server.TOOLS`（schema）、`engine.TOOL_HANDLERS`（实现）、`protocol.TOOL_GUIDE`（提示词）；
   `test_network_tools_defined_consistently` 会在漏改时变红。工具里的自由文本参数一律用 `string`，不要放进数组
   （DeepSeek 下数组参数里的长中文容易写成裸文本，导致 `MALFORMED_RESPONSE`）。
6. 状态结构有变化时保证 `Store._load` 能读旧的 `state.json`（`store._known` 会丢弃未知字段、缺失字段取默认值）。
7. 规则反复靠记忆失效时，把它挪进代码或测试（参照 Raft 的 structural enforcement）：一条规则如果违反了也没有任何信号，
   就要么加一个能变红的检查，要么写明为什么接受这个风险。
8. 不要提交 `.agentnet/` 下的运行数据；那里可能包含真实研究结果与会话记录。
9. 依赖使用精确或下限版本，经 `uv add` 写入 `pyproject.toml` 与 `uv.lock`。

### 三、安全边界

- `/api/*` 要求登录（邮箱 + 密码，`auth.py`，HttpOnly + SameSite=Lax cookie），按用户做归属与可见性检查。
  这仍是最简实现：没有 HTTPS、CSRF 令牌、登录限流、令牌过期、邮箱验证。服务只监听 `127.0.0.1`，不要暴露到公网或改成 `0.0.0.0`。
- 密码只存 scrypt 哈希；不要在日志、事件或 API 返回里带出密码、哈希、登录令牌。`/api/users` 只返回 id 与邮箱。
- `/internal/tools`、`/internal/approval` 只能凭有效执行上下文令牌调用，不走登录；不要放宽这一校验。
- 没有 `file.write` 的 Agent 运行在只读沙箱；有则只能写自己的工作目录。不要绕过 `build_patch_yaml` 生成的限制，
  也不要让审批应答者自动批准：工作目录以外的写只能由 Agent 所有者逐次批准。
