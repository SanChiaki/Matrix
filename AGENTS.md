# AGENTS.md

给在本仓库工作的 AI Agent 的项目说明与工程约束。动手前先读完本文件。

项目方向与设计理念见 [CHARTER.md](CHARTER.md)（行动纲领原文）。本文件讲"当前代码怎么做"，
CHARTER 讲"我们要往哪走"。需要判断某个改动是否该做时，以 CHARTER 的方向为准。

竞品与相关项目的调研不在本仓库：它们在一个私有调研仓库里，检出在本仓库**旁边**（`../matrix-research`，
没有就跳过）。本仓库里写“参照 X”的地方带一个引用 `research: <笔记>#<锚点>`，指向那里的 `products/<笔记>.md`
或 `topics/<笔记>.md` 中的 `<a id="锚点">`。要了解某个设计从哪里学来、当时为什么这么定，按引用去读；
调研仓库在手边时，动相关设计之前先读对应笔记。**调研内容不要抄进本仓库**（本仓库公开），只写结论与引用。

## 项目描述

**Matrix**（代码包名 `agentnet`）：一个本地运行的 Agent Network（Agent 协作网络）Demo。人在群聊里 @ Agent 交代任务（或和单个 Agent 私聊）；被 @ 的 Agent 自己做，或通过网络工具（MCP）把子任务委托给同组成员，网络负责授权、隔离、追踪与知识沉淀。

- 语言 / 依赖：Python ≥ 3.14，FastAPI + uvicorn，包管理用 `uv`，测试用 pytest。
- 运行时：默认以 DeepSeek Harness（DSH）headless 为 Agent 运行时；`scripted` 运行时是确定性脚本，用于测试与离线演练。
- 状态：全部持久化在 `--data` 目录（默认 `.agentnet/`，已被 gitignore）的 `state.json`；对话、任务各有独立工作目录 `conversations/<id>/`、`tasks/<id>/`；每个 Agent 有持久私有目录 `agents/<id>/`；每个群组有档案目录 `groups/<id>/`（`files/` 与变更日志 `log.jsonl`），不放进任何项目仓库。

### 领域模型（`agentnet/models.py`）

| 概念 | 含义 |
| --- | --- |
| User | 邮箱 + 密码（scrypt）。第一个注册的用户接管已有的无主数据 |
| Agent（节点） | 名称、描述、能力、**自身权限**（运行时上限）、是否允许私聊、可选的运行时模型（provider / model / reasoning_effort，空则用运行时默认）；**所有者**。Agent 只属于所有者、只为所有者工作（不变量 16），没有 public / shared。系统不管理注入 Agent 上下文的经验 |
| Group（群组） | 成员集合，**没有主 Agent**。`internal`（内部群组）：只有所有者，成员只能是所有者自己的 Agent。`joint`（联合群组，参照 Raft 的 joint channel，research: raft#joint-channel）：主持人（`owner_id`）邀请用户，接受后成为参与者（`participants`），每人只能加入自己的 Agent。创建后不能转换类型。委托只能发给同组成员；网络**不会**自己创建 Agent，只接受 Agent 的提议、由人批准。每个群组带一份**群组档案**（见下） |
| Conversation | 参照 Raft 的频道与 DM（research: raft#channels-dm）：`group`（群聊）**每个群组一个**，建群组即有，人在消息里 **@ 谁，谁接任务**；`dm`（私聊）**每个 Agent 一个**，只有它的所有者能开，第一次打开时创建，不用 @，不能委托。群组 / Agent 删除后对应对话归档为只读。消息的 `audience` 限定谁看得到这条（如席位失败说明、外溢申请），`private_to` 限定谁看得到它的过程（`steps` / `wake`） |
| Approval | Agent 想在工作目录以外操作时向**它的所有者**发起的一次性申请（pending / allowed / rejected / cancelled） |
| Seat（席位） | 一个 Agent 在一个对话里的执行上下文，id 为 `对话/Agent`：独立 DSH 会话、收件箱、工作目录 `conversations/<conv>/<agent>/`。第一次被 @ 时创建 |
| Task（委托） | Agent ↔ Agent 的一次授权：mission / authority / constraints / expected_outcome / share；带收件箱。`owners` 是两端 Agent 的所有者，只有他们看得到这个委托 |
| KnowledgeItem | 任务结果沉淀为知识，key 形如 `<task_id>:result` |
| UserRequest（用户任务） | 人交代给 Agent 的一件**有交付目标**的事，看板上的一张卡。由接任务的 Agent 判断并登记（`open_request`）：闲聊、问答不算。来源消息与下发人由网络从这一轮送达的 @ 它的消息里认定，不信任模型自报。`in_progress → in_review → completed`（或 `cancelled`）；交付（`deliver_request`）前它为此发起的委托必须都有结果；下发人验收或退回（原会话返工）。为它发起的委托记在 `task_ids`，只在任务详情里展示 |
| Proposal | Agent 提议新建的 Agent，由**提议者 Agent 的所有者**审核（`owner_id`），新 Agent 归他；`max_authority` 记录提议者当时的有效权限 |
| 群组档案 | 归群组所有：`brief`（目标、范围、成功标准、当前阶段，≤4000 字，**每轮推送**）、`decisions`（有效 / 被取代 / 撤销，推送最近的有效决定）、文件（`groups/<id>/files/`，每轮只读拷进工作目录的 `group/`，运行后丢弃改动）、`refs`（资源引用：仓库 / 路径 / 文档，只是指针，不授予访问权限；旧版叫 `projects`）、`suggestions`（brief 修改建议）。`archive_rev` 每变一次 +1，席位 / 任务只在版本变化时重新推送 |

### 群聊：被 @ 的成员接任务

参照 Raft（botiverse/raft）的频道模型（research: raft#channels-dm）：

- 人发的消息里 @ 到的群组成员（`@名称`，长名字优先匹配；或 API 的 `mentions`）被唤醒，在自己的席位上处理。它自己做，或在群组里找合适的成员委托。一条消息可以 @ 多个成员，各自的席位并行处理。
- 没 @ 任何人的消息留在群聊里，不唤醒任何人；只能 @ 群组成员。
- 席位被唤醒时收到**上次运行之后群聊里的新公开消息**（人的消息、其他成员的回复），其中 @ 它的那条标为“@ 你”。委托的过程、结果、知识不在公开消息里，只送给委托方（不变量 9）。
- 委托回报回到委托方的席位（或上一级任务），由它验收。执行者工作目录里的**产物**（除去运行时文件、只读副本、`tmp/`、缓存）
  在交付时快照进委托方工作目录的 `deliveries/<task_id>/`（`agentnet/workdir.py`），重新交付时整体替换。
  Agent 的所有者可以在 UI 里浏览、下载自己 Agent 的席位与任务工作目录（只读）。
- 群成员随时可以加减（`POST /api/groups/<id>/members`、`DELETE …/members/<agent>`），变动作为 `event` 消息写进群聊，
  之后被唤醒的成员也能看到。移出群组不打断已经接下的任务，只是不能再被 @、被委托。
- 群聊、私聊不能删除，只能清空记录（`/clear`，席位会话从头开始，委托与知识不受影响）；只有归档的对话可以删除。
- 旧版对话（一个“主 Agent”）加载时迁移为只有这一个席位的群聊（原来在群组里）或私聊，沿用原会话与目录。
  旧版同一个群组 / Agent 有多个对话时，最近活跃的那个成为正式群聊 / 私聊，其余归档为只读（`Store._one_chat_each`）。
- 旧版 public / shared Agent 加载时（`Store._one_trust_domain`）：别人和它的私聊归档为只读；内部群组里不属于群组所有者的 Agent
  移出，群聊里记一条事件；已有委托与记录保留。

### 联合群组：跨用户协作只走这里

参照 Raft 的 joint channel（research: raft#joint-channel）：**Agent 不跨信任域，委托可以跨**。共享的是对话空间（群聊 + 群组档案），不共享 Agent。

- 主持人按邮箱邀请（`POST /api/groups/<id>/invitations`），对方在“待我处理”里接受或拒绝。接受前只看得到群组名、描述、主持人。
- 每个参与者只能加减自己的 Agent；主持人能移出任何 Agent 与参与者。参与者退出 / 被移出时他的 Agent 一并移出（已接下的任务照常完成），
  之后看不到群组与群聊。主持人不能退出，只能删除。改名、brief、资源引用、清空群聊、删除群组只有主持人能做。
- 任何参与者能 @ 任何成员。被 @ 的 Agent 仍**以它的所有者的名义、用所有者的资源**运行：外溢审批找它的所有者，提议由它的所有者审核。
- 可见性（不变量 13）：人的消息、Agent 的最终回复、成员与档案事件对所有参与者可见；Agent 回复的过程（`steps` / `wake`）、运行中的进度、
  席位失败说明、外溢申请只给这个 Agent 的所有者；委托（详情、看板、事件）只给两端 Agent 的所有者（`Task.owners`）。
- 私有目录默认**不挂载**（其他参与方的消息是不可信内容，群聊是外泄通道），所有者可以按（Agent, 群组）打开（`home_enabled`）。
- 工作台账在联合群组里只列同一群组的条目，其余只给数量（不变量 16）。

### 项目：目标与汇总层（设计见 [docs/projects.md](docs/projects.md)）

- `Project`：一个所有者、brief、方面（`workstreams`）。内部群组挂进项目并承担一个方面（`Group.project_id` / `workstream_id`，
  一个群组最多属于一个项目）；群组与项目必须同一个所有者，**联合群组暂时不能挂**。
- **项目汇总工作，不打通群组**：群组、委托、知识、可见性规则都不因项目改变（不跨群组委托、群聊不互通）。
  流向 Agent 的只有两样，**只到挂载群组**（不变量 18）：项目背景（名称、brief、方面与承担的群组、“你所在群组负责 X 方面”，
  `format_project`，按 `Project.archive_rev` + 方面推送，`Seat/Task.project_seen`）和项目文件（`projects/<id>/files/`，
  每轮只读拷进工作目录的 `project/`，端侧随 `materials` 下发；写入只经 `publish_file target=project`、人上传或把群组文件提升到项目，
  记入 `projects/<id>/log.jsonl`）。人对项目的操作不写进群聊；Agent 发布到项目时在来源群聊记一条事件。
- **跨群组交接**（`handoff`，项目工具，挂项目的群组里才有，不需要 `delegate`）：Agent 不能跨群组委托或发言，
  只能提交交接；项目所有者在“待我处理”或项目页确认（可改内容）后，以人的名义发进目标群聊并 @ 目标成员（不变量 19）。
  发出只放进提交者的收件箱、不唤醒它；不采纳时唤醒它；由它引出的用户任务验收通过时唤醒它（`handoff_done`）。
  交接可以带依赖（`after`）：确认后排队（`scheduled`），依赖都完成时由网络以确认人的名义发出。
  `replaces` 替换一条还没发出的交接（旧的 `superseded`，等它的改为等新的）。进行中的交接写进项目背景。
- **负责人统筹**（docs/projects.md §9）：所有者可以指定项目负责人（挂载群组的成员）并打开统筹（`Project.autonomy = lead`），
  交接改由负责人在它那个群组的群聊席位里用 `review_handoff` 确认（负责人工具 `LEAD_TOOLS`，`ExecContext.lead`）；
  负责人自己提交的不重复的交接直接发出。越界的转给人（不变量 20），人的待办里只出现等人确认的交接。
- **项目决定**（`Project.decisions`）：只有所有者记录，推送给所有挂载群组，和群组决定冲突时以它为准。
- 用户任务的项目归属由网络按所在群组认定（`open_request` 时），不让模型填；群组换方面 / 移出时未结束的任务跟着改，
  已结束的保留历史。私聊里的任务由下发的人手动归类（`assign_request`），联合群组里的任务不能归类。
- 项目只有所有者看得到（无权限按 404）；`project.*` 事件只推给所有者（`event_view`）。

### 群组档案：群组的上下文不放进项目仓库

- Brief 与仍然有效的决定作为背景推送给席位与委托任务（执行者是群成员，档案是群组的公开内容，不违反不变量 9）；只在档案版本变化时重新推送。
  提示词里不写“先读档案”之类的指令（参照 Raft RFC 070：宣布状态重建会让 Agent 先做整理仪式而不是处理工作；research: raft#rfc-070-session-recycle）。
- 档案工具 `record_decision` / `list_decisions` / `publish_file` / `propose_brief` 群组成员都有，**不需要 `delegate`**；
  用户任务工具 `open_request` / `deliver_request` / `accept_request` 席位都有；其余网络工具（协作工具）需要 `delegate`（`engine.network_tools_for`）。MCP 服务只列出这次能用的工具（`AGENTNET_TOOLS`），网络端再按执行上下文检查一次。
- `publish_file` 只能发布当前工作目录里的文件（不含 `group/`、`project/` 与 `agent-home/`）；`group/` 是只读副本，运行里的改动丢弃。
- Brief 主持人直接改，其他参与者与 Agent 用 `propose_brief` / “建议修改”提建议，主持人采纳或拒绝。brief 超过 14 天没确认，UI 提醒主持人复核。
- 联合群组的档案对所有参与者可见：写进去就等于对所有参与方公开（工具描述与提示词里写明）。

### 看板：只有人交代的任务

- 看板（`/api/board`）只列 UserRequest，不列 Agent 之间的委托（委托在任务详情里看）。
- 群聊被 @、私聊消息都可以成为任务。是不是任务由接任务的 Agent 判断（提示词 `protocol._requests`），登记、交付、退回都写一条 `event` 进对话。
- `open_request` / `deliver_request` / `accept_request` 只有席位（群聊、私聊）有，不需要 `delegate`；委托任务里没有。
- 看得到任务的人：下发的人，以及接任务的 Agent 的所有者（内部群组、私聊里是同一个人）。只有下发的人能验收、退回、取消；也可在本轮送达且 @ 接任务席位的消息里明确验收，由该席位用 `accept_request` 代办（记录消息依据，不开放退回或取消）。

### 云侧与端侧运行时（方向见 [docs/device-runtimes.md](docs/device-runtimes.md)）

- Agent 可以跑在**平台云侧**（内置 dsh）或**用户端侧**（用户自己的 Claude Code / Codex 等 harness）。
  网络对两者**同等看待**：位置只是节点属性，不改 Seat / Task / Approval / 知识 / 群组成员的语义。
- 端侧的接入方式是用户电脑上的**出站连接器**（不开入站端口），按用户配对：一个用户可以签发**多条**
  设备令牌（`POST /api/devices`，只存 sha256，可单独吊销），连接器用令牌连 `GET /api/devices/connect`
  （WebSocket，令牌走 query）。网络工具经连接器回传，身份仍由执行上下文令牌认定；
  审批上行进入现有 Approval 记录，由 Agent 的所有者决定。
- **有效权限要再交运行时强制执行能力**：只有该 harness 能强制的权限才进入有效权限，
  只能靠提示词约定的按 `advisory` 记账、自动收窄，并在委托详情里写明原因。
- 私有目录在端侧就留在用户电脑上（`<root>/.homes/<agent>/`），网络不读、不注入；服务器只在 `run.request.home`
  里说明挂载方式（`rw` / `ro` / 不挂载），连接器按与云侧相同的语义（`agentnet/homedir.py`）挂载与写回。群组文件按轮下发只读副本。
- **端侧不可达是等待，不是失败**：任务进入 `WAITING`，不消耗重试，无限等；对话状态仍是 `waiting`；
  端侧席位只给所有者写一条“已排队”说明。上线后（`register_runtime`）唤醒该设备上等待中的任务，消费积压。
  离线期间按最近一次握手声明的能力计算有效权限（`Agent.enforcement`），不把“未知”当成“全都不行”。
- **A2A 只用于网络边界**（外部 A2A 网络、多套 Matrix 之间），内部 Agent 协作仍然只走网络工具；
  外部请求进来的任务不获得本系统内的授权。
- 当前状态：**运行时契约、能力报告、连接器协议与配对已实现**（`runtime.RuntimeCapabilities`、
  `engine.Network.capabilities`/`_effective`/`runtime_for`、`Task.narrowed`、`device.py`、设备令牌与
  `/api/devices/connect`）。**连接器本体已实现**：`uv run matrix-connect`（`agentnet/connector/`），dsh、
  Claude Code、Codex 三个驱动，已用本机 dsh 与 Claude Code 跑通端到端。**端侧其余部分也已实现**：
  设备管理页（签发 / 吊销令牌、在线状态）、Agent 绑定设备与能力缺口展示、材料下发（`run.request.materials`）
  与 `publish_file` 回传（`file.fetch`）。**还没有**：云侧的读隔离（需求 #34）。

### 执行模型：异步委托 + 收件箱唤醒

参照 Raft 的 inbox + wake 模型（research: raft#inbox-wake）：

- `delegate` **立即返回**任务 id，不阻塞。每个执行上下文（席位、任务）有一个收件箱；收件箱有内容且没在运行时，网络续接它的会话跑一轮。同一上下文同时只跑一轮，运行中到达的内容等这一轮结束再送达。
- 任务状态：`running → in_review → completed`，另有 `failed`（可续接）、`rejected`（授权阶段被拒）。交付进入 `in_review`，交付通知送进委托方收件箱；委托方 `accept_task` 或 `request_changes`（执行者在原会话返工）。送达那一轮结束仍未处理的交付视为接受（`auto_accepted`）。
- 执行者自己还有进行中的子任务或待审核提议时，这一轮的回复只记为进展（`task.notes`），等回报唤醒后再交付。
- 对话状态：`thinking`（有席位在跑一轮）/ `waiting`（有进行中的委托）/ `idle`。
- 失败语义：运行时错误按类别处理（`runtime.classify_error`）；`MALFORMED_RESPONSE`、`EMPTY_RESPONSE` 用同一会话自动续接一次。仍失败时这个**席位**进入 `blocked`（其他席位不受影响），失败消息列出它已完成的委托与已入库知识；人点“继续”或再 @ 它，同一会话从中断处接着做。失败的任务由委托方 `retry_task` 或人在 UI 上续接。

权限词表：`web.research`、`file.write`、`shell.exec`、`delegate`。

### 模块地图

| 文件 | 职责 |
| --- | --- |
| `agentnet/engine.py` | `Network` 核心：Agent/群组/对话管理、联合群组的参与者与可见性、群组档案、@ 解析、收件箱调度、席位与任务轮次、委托授权、验收与续接、提议审核、私有目录挂载、知识可见性、违规检测 |
| `agentnet/protocol.py` | 对话 / 任务 / 通知提示词、交付与失败说明、`TOOL_GUIDE`（网络工具用法） |
| `agentnet/runtime.py` | `AgentRuntime` 抽象；`DshRuntime`（`--patch` 覆盖层按权限禁用插件 + 沙箱 + 挂载 MCP + 按 Agent 设模型）；`ScriptedRuntime`；运行时错误分类 |
| `agentnet/mcp_server.py` | 仅标准库的 stdio MCP 服务，`TOOLS` 是网络工具 schema 的唯一定义，回调 `/internal/tools` |
| `agentnet/server.py` | FastAPI：登录、REST（按用户过滤）、看板 `/api/board`、SSE 事件流 `/api/events`、静态页面、运行时回调 `/internal/tools` 与 `/internal/approval` |
| `agentnet/auth.py` | 用户注册 / 登录 / 退出（scrypt，仅标准库）；第一个注册的用户接管已有数据 |
| `agentnet/dsh_plugins/approval.mjs` | dsh 审批应答者：沙箱升权申请转给网络，由 Agent 所有者决定 |
| `agentnet/connector/` | 端侧连接器 `matrix-connect`：出站 WebSocket、本机桥（`/internal/*` 转发出站）、dsh / Claude Code / Codex 驱动 |
| `agentnet/workdir.py` | 工作目录里的“产物”口径：委托产物快照（`deliveries/`）、目录打包发布、人浏览工作目录 |
| `agentnet/homedir.py` | 私有目录的挂载与三方合并写回，云侧（engine）与端侧（connector）共用 |
| `agentnet/store.py` | 持久化、旧数据迁移（`_one_chat_each`、`_one_trust_domain`）与种子数据（5 个种子 Agent + 「AI 产品机会研究组」；服务默认不预置，CLI 与测试预置） |
| `agentnet/events.py` | 进程内事件总线（带 seq，供 SSE 与 CLI 消费） |
| `agentnet/scripted.py` | 脚本运行时的场景处理器 |
| `agentnet/cli.py` | 无 UI 跑一轮对话并打印事件流 |
| `agentnet/static/` | 单页前端（原生 JS） |
| `tests/` | `test_network.py`（引擎不变量、结构检查）、`test_api.py`（HTTP + MCP 回调）、`test_users.py`（登录、信任域、外溢审批、台账、看板）、`test_joint.py`（联合群组、群组档案）、`test_projects.py`（项目） |
| `REQUIREMENTS.md` | 全部需求、实现状态与核心思路 |
| `docs/device-runtimes.md` | 云侧 / 端侧运行时、运行时能力报告、可达性与信任边界（草案） |
| `docs/projects.md` | 项目：目标与汇总层的设计与分阶段计划 |

### 常用命令

```bash
uv sync                                         # 安装依赖
uv run pytest -q                                # 全部测试（离线，不需要 dsh）
uv run agentnet --runtime scripted              # 启动服务 http://127.0.0.1:8765（离线）
uv run agentnet                                 # 使用 DSH 运行时（需 PATH 中有 dsh）
uv run python -m agentnet.cli "我要研究一个新的 AI 产品机会。" --runtime scripted   # 在群聊里 @ Product Strategy Agent
uv run matrix-connect --url ws://127.0.0.1:8765/api/devices/connect --token <设备令牌> --driver dsh   # 端侧连接器
```

## 工程约束

### 一、网络不变量（改代码时必须保持，均有测试覆盖）

1. **权限只收窄不放大**：执行者有效权限 = 执行者自身权限 ∩ 委托授予；授予只能来自委托方的有效权限。
   还要再交**运行时能强制的权限**（`RuntimeCapabilities.enforces`）：做不到的不进入有效权限，
   记进 `Task.narrowed` 并写明原因。见 `docs/device-runtimes.md` 第 3 节。
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
10. **身份由网络认定**：调用方由运行期间才有效的执行上下文令牌识别，不信任模型自报身份。用户任务代验收只认本轮送达、@ 当前席位的下发人消息，验收语义由模型判断。
11. **越权必留痕**：工具调用与权限不符时写入 `task.violations` 并发出 `authority.violation` 事件。
12. **重启语义**：重启后对话回到 `idle`（席位收件箱保留）；运行中的**云侧**任务标记为 `FAILED`，失败通知进委托方席位或
    上级任务的收件箱，可续接；待审批的外溢申请作废（那次运行已随重启结束）。**端侧**任务回到 `WAITING`（不判失败；
    `Store._load` 按 `delegatee.device_runtime` 区分），端侧上线后继续。
13. **归属与可见性**：用户只看得到自己的内部群组、自己参与的联合群组及其群聊、自己的私聊、自己的 Agent（和同在联合群组里的别人的
    Agent 的名片）。委托只给两端 Agent 的所有者；联合群组里过程、失败说明、外溢申请只给 Agent 的所有者；提议给提议者 Agent 的所有者；
    知识随产生它的委托。无权限一律按“不存在”（404）处理。SSE 推送前按用户过滤并裁剪（`Network.event_view`）。
    引擎方法的 `actor=None` 表示系统内部或测试，不做检查；HTTP 层必须传入当前用户。
14. **工作区外溢由所有者批准**：dsh 沙箱拒绝工作目录以外的写之后，模型可以申请升权重试一次；申请经 `dsh_plugins/approval.mjs`
    转到 `/internal/approval`，交给 **Agent 的所有者**（不是发起对话的人）。获批只对那一次调用生效；云侧超时
    （`approval_timeout_s`）作废，**端侧不按时间作废**，只在设备断连时结束。
15. **并发不抢占**：同一 Agent 可同时做多件事（包括在联合群组里被别人 @）。每轮提示词带工作台账（`Network.ledger`）；私有目录写回时按文件三方合并，
    期间被别的运行改过的文件不覆盖，另存 `*.conflict-*` 副本。
16. **Agent 不跨信任域**：Agent 只属于所有者，只有所有者能用它（私聊、拉进群组）、改它；它只代表所有者运行。跨用户协作只能通过联合群组。
    联合群组里私有目录默认不挂载，工作台账不列出别处工作的细节。
17. **群组档案只走网络**：写入只能经人的操作或档案工具，变更追加到 `groups/<id>/log.jsonl`，公开变化写进群聊；运行时拿到的是只读副本。
    推送部分有预算（brief ≤4000 字，最近 12 条有效决定）。
18. **项目推送只到挂载群组**：项目背景与项目文件只推送 / 挂载给挂载在这个项目里的（内部）群组的席位与委托任务；
    私聊、未挂载的群组、联合群组拿不到。项目只有一个所有者、挂载群组与项目同一个所有者，所以不跨信任域。
19. **跨群组只经人或项目负责人**：Agent 不能直接跨群组委托或在别的群聊里发言；只能向同一项目里的其他群组提交交接（`handoff`），
    由项目所有者确认，或在所有者打开负责人统筹时由项目负责人 Agent 在它统筹的群聊席位里确认（身份由网络认定）；
    都以所有者的名义发出，消息里写明是谁确认的。
20. **负责人统筹有边界，越界转给人**：负责人确认的交接每 24 小时有总数与每对群组的上限（`Limits.lead_daily` / `lead_pair`），
    超了、负责人席位暂停、负责人一轮收到却没处理、或它要求转交时，交接转给项目所有者。所有者随时能直接处理任何交接；
    改回人工或换负责人时，还等负责人确认的一律转给人。负责人不能验收用户任务、改 brief 或记项目决定。

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
7. 每条网络不变量都要有测试，测试 docstring 写“不变量 N”；`test_invariants_have_tests` 在新增不变量却没写测试时变红。
8. 规则反复靠记忆失效时，把它挪进代码或测试（参照 Raft 的 structural enforcement，research: raft#structural-enforcement）：一条规则如果违反了也没有任何信号，
   就要么加一个能变红的检查，要么写明为什么接受这个风险。
9. 不要提交 `.agentnet/` 下的运行数据；那里可能包含真实研究结果与会话记录。
10. 依赖使用精确或下限版本，经 `uv add` 写入 `pyproject.toml` 与 `uv.lock`。
11. 设计借鉴了别的产品时写“参照 X”并带 `research: <笔记>#<锚点>`（见文首），调研笔记里没有的先补笔记。
    `test_research_refs_resolve` 在调研仓库就位时检查每个引用都能找到对应的笔记与锚点，不在时跳过。

### 三、安全边界

- `/api/*` 要求登录（邮箱 + 密码，`auth.py`，HttpOnly + SameSite=Lax cookie），按用户做归属与可见性检查。
  这仍是最简实现：没有 HTTPS、CSRF 令牌、登录限流、令牌过期、邮箱验证。服务只监听 `127.0.0.1`，不要暴露到公网或改成 `0.0.0.0`。
- 密码只存 scrypt 哈希；不要在日志、事件或 API 返回里带出密码、哈希、登录令牌。`/api/users` 只返回 id 与邮箱。
- `/internal/tools`、`/internal/approval` 只能凭有效执行上下文令牌调用，不走登录；不要放宽这一校验。
- **网络管关系，不管 Agent 在自己环境里能碰什么**。云侧 Agent 的读隔离目前做不到（见下一条）；端侧 Agent
  读的是所有者自己的机器，不在网络管辖范围，网络只对经过它的协作工具负责。联合群组里端侧 Agent 会把群聊内容
  带到成员的机器上——这是已接受的披露（与"档案对所有参与方可见"同一逻辑），UI 要标出成员所在位置。
- **已知风险（接受）：沙箱只管写、不管读**。任何 Agent 的运行时都能读到整个 `--data` 目录（别人的私有目录、对话、任务、群组档案）。
  信任域隔离（不变量 13、16）目前只在提示词、网络工具与 API 层成立，文件层挡不住；联合群组会放大这个风险（别的参与者可以 @ 诱导读取）。
  接受理由：本地单机 Demo、所有用户在同一台机器上；范围：DSH 运行时；复查时机：接入第二个运行时或任何多机部署之前，必须先做读隔离。
- 群组档案文件经 `/api/groups/<id>/files/<name>` 一律以附件下载（`nosniff`），不在本站点内渲染；文件名经 `_safe_filename` 只取最后一段。
- 没有 `file.write` 的 Agent 运行在只读沙箱；有则只能写自己的工作目录。不要绕过 `build_patch_yaml` 生成的限制，
  也不要让审批应答者自动批准：工作目录以外的写只能由 Agent 所有者逐次批准。
