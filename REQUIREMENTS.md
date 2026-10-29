# Matrix 需求清单

按提出的时间排列。状态：✅ 已完成 · 🟡 部分完成 / 有已知限制 · ⬜ 未开始 · ↩️ 已被后续需求取代。
工程约束与不变量见 [AGENTS.md](AGENTS.md)，方向见 [CHARTER.md](CHARTER.md)。

## 一、基础与界面

| # | 需求 | 状态 | 核心思路 |
|---|---|---|---|
| 1 | 修复前端布局错乱（纵向未填满） | ✅ | 网格布局给出明确高度，主区域 `min-height: 0` 允许内部滚动；静态文件改成 `no-cache`，避免浏览器沿用旧样式 |
| 2 | 对话列表放左侧栏，会话对象 / 群组的选择放中央 | ✅ | 已被 #17 的频道式侧栏取代 |
| 3 | 对话支持 Markdown 渲染 | ✅ | 零依赖的 `static/md.js`，先转义再渲染，链接只允许 http(s) |
| 4 | 会话进度实时刷新 | ✅ | `run.progress` 事件（中间文本 + 工具调用）经 SSE 推送；对话 / 委托详情按节流重拉 |
| 5 | 不在系统里管理注入 Agent 上下文的经验（移除 Agent Memory） | ✅ | 删除 memory 字段与注入；旧 `state.json` 读入时丢弃。由 #10 的私有目录替代 |
| 6 | 接入 GitHub 仓库 SanChiaki/Matrix | ✅ | |

## 二、稳定性（MALFORMED_RESPONSE 排查）

| # | 需求 | 状态 | 核心思路 |
|---|---|---|---|
| 7 | 找出对话反复失败的本质原因（不是模型问题） | ✅ | 三层叠加：自由文本塞进数组参数、dsh 一个 step 里一个调用坏就整步作废、Matrix 一轮跑完整个编排导致失败代价巨大 |
| 8 | 改 delegate 的 schema | ✅ | 约束 / 共享改为可选的扁平字段，长内容不再走工具参数 |
| 9 | 失败可续接、不重来 | ✅ | 错误分类（`runtime.classify_error`），可恢复的错误用同一会话自动续接一次；仍失败则席位 `blocked`，人点“继续”或再 @ 它 |

## 三、参考 Raft 的改造

| # | 需求 | 状态 | 核心思路 |
|---|---|---|---|
| 10 | Agent 私有目录（替代 Memory） | ✅ | `agents/<id>/` 每次运行挂载为 `agent-home/`，网络不读不注入；并发时按文件三方合并，冲突另存副本（#26） |
| 11 | 异步委托 + 收件箱唤醒 | ✅ | `delegate` 立即返回；交付 / 失败作为通知进委托方收件箱，网络续接其会话跑一轮。同一上下文串行，不同上下文并行 |
| 12 | 委托方验收 | ✅ | 交付进入 `in_review`；`accept_task` / `request_changes`（原会话返工）/ `retry_task`；没表态则一轮结束视为接受 |
| 13 | Agent 提议新建 Agent，人审核（Action Card） | ✅ | `propose_agent`，权限不超过提议者当时的有效权限；人可改字段后批准，以人的身份创建并入群 |
| 14 | 按 Agent 配置模型 | ✅ | `--patch` 覆盖 `agent-default-model`；UI 显示实际使用的模型 |
| 15 | 结构化约束代替“记得同步” | ✅ | 测试保证网络工具三处定义一致、权限三处登记一致 |
| 16 | 允许 runtime 自带的 subagent / workflow | ✅ | 系统可见的 Agent 之间协作走网络；Agent 自己的子 Agent 不管控、不记违规。只禁用能绕过覆盖层的 `tool-plugin-manager` |
| 17 | 群组改造：没有主 Agent，@ 谁谁接任务 | ✅ | 每个被 @ 的成员在群聊里有独立席位（会话 / 收件箱 / 目录）；被 @ 后自己做或在群里找人委托；成员看得到群里的公开消息 |
| 18 | 群组 = 群聊（频道），点进去就聊；成员随时加减；私聊 = 和单个 Agent 的会话 | ✅ | 每个群组一个群聊，建群即有；成员变动作为事件写进群聊；旧版多余对话归档为只读 |
| 19 | 产品名定为 Matrix，画 logo | ✅ | `static/logo.svg`：3×3 节点矩阵，连线写出 M |
| 20 | 没聊过的 Agent 不出现在私聊列表；输入框不列出所有成员；卡片上直接有“对话”按钮 | ✅ | 输入 `@` 弹出补全；卡片底部“对话”直达群聊 / 私聊 |
| 21 | 卡片悬停的怪高亮 | ✅ | 卡片内容区不再继承全局按钮的悬停色 |
| 22 | 收件箱对用户可见 | ⬜ | 已说明方案（详情接口返回待送达摘要、排队消息标记），等确认 |
| 23 | 调整主 Agent 倾向于把所有成员都叫一遍 | 🟡 | 提示词改为“先判断自己能不能做，只委托确实需要的部分”；#17 之后不再有固定的主 Agent |

## 四、多用户（本次）

| # | 需求 | 状态 | 核心思路 |
|---|---|---|---|
| 24 | 用户：邮箱 + 密码；登录后引导创建首个 Agent | ✅ | `matrix/auth.py`：scrypt 哈希（仅标准库），登录令牌存 `state.json`，经 HttpOnly + SameSite=Lax cookie 传递。`/api/*` 都要登录。没有自己的 Agent 时显示“创建你的第一个 Agent”，创建后直接打开和它的私聊。首个 Agent 默认成为这个人的**助理**（可以换，见 #43）：进项目时自动加入，项目会话里陪这个人聊。第一个注册的用户接管已有数据。**限制**：没有邮箱验证、找回密码、令牌过期、登录限流、CSRF 令牌 |
| 25 | 默认只看到自己的 Agent 和群聊；Agent 可公开或分享给指定用户 | ↩️ 已被 #30 取代 | Agent 有 `owner_id` / `visibility`（private · public · shared）/ `shared_with`。能用的人可以私聊、拉进自己的群；只有所有者能改。群组与群聊只对所有者可见；私聊按（用户, Agent）各一个。委托、提议、看板随对话归属。SSE 事件按用户过滤。无权限一律返回 404，不泄露存在性 |
| 26 | Agent 工作区限制外溢：工作目录以外的操作向所有者申请（dsh 是否默认支持？） | 🟡 | **dsh 支持，但 headless 默认没人应答，等于永远拒绝。** 沙箱拒绝后，模型可带理由申请升权重试一次，dsh 把申请交给 `approval/request` 应答者。我们写了应答插件 `matrix/dsh_plugins/approval.mjs`，经 `--patch` 挂载，把申请转到 `/internal/approval`，由 **Agent 所有者**（不是发起对话的人）在看板或对话里批准或拒绝；15 分钟没人处理就作废。已用真实 dsh 验证写入成功。**限制**：沙箱只管写，读不受限；获批是“这一次调用以完全访问权限运行”，不是放行某个路径 |
| 27 | Agent 支持多人并发对话，避免并行工作互相抢占 | ✅ | 不限并发：不同用户的私聊、不同群聊席位、不同委托各有独立会话，并行运行。**工作台账**：每轮提示词告诉 Agent “你同时还在处理：谁的什么事”，看板也展示。私有目录写回时按文件三方合并，别的运行在这期间改过的文件不覆盖，另存 `*.conflict-*` 副本 |
| 28 | 任务看板（参考 Raft） | ↩️ 已被 #35 取代 | 导航「看板」：按 执行中 / 待验收 / 已完成 / 失败·已拒绝 分列，可按 Agent 筛选，点卡片看详情（复用委托详情，失败可续接）。顶部「待我处理」放外溢申请和 Agent 提议，导航上显示待办数；底部「正在工作」展示工作台账 |
| 29 | 把需求整理到一个文件，标注状态与思路 | ✅ | 本文件 |

## 五、信任域、联合群组、群组档案（参考 Raft joint channel、Kylon）

| # | 需求 | 状态 | 核心思路 |
|---|---|---|---|
| 30 | Agent 不跨信任域 | ✅ | Agent 只属于所有者、只为所有者工作：去掉 public / shared，只有所有者能私聊、拉进群组、修改。旧数据迁移：别人和它的私聊归档，内部群组里别人的 Agent 移出并记事件（不变量 16）。#25 的“分享给别人用”由 #31 取代 |
| 31 | 区分内部群组与联合群组 | ↩️ 已被 #50 取代 | 内部群组只有你和你的 Agent；联合群组由主持人邀请、对方接受，各自带上自己的 Agent。任何参与者能 @ 任何成员，Agent 仍以所有者的名义、用所有者的资源运行。委托只给两端的所有者看；过程、失败说明、外溢申请只给 Agent 的所有者；台账不列别处的细节；私有目录默认不挂载，所有者按群组打开；参与者退出后看不到群组与群聊 |
| 32 | 群组上下文：不放进某个项目仓库 | ✅ | 群组档案 `groups/<id>/`：brief（主持人直接改，其他人提建议，每轮推送）、决定（有效 / 被取代 / 撤销，推送有效的）、文件（网络工具发布，只读挂载）、项目引用（只是指针）。写入只走网络并记日志（不变量 17）。档案工具不需要 `delegate` |
| 33 | 每条不变量都有能变红的测试 | ✅ | 测试 docstring 写“不变量 N”，`test_invariants_have_tests` 对照 docs/reference/invariants.md |
| 34 | 读隔离 | ⬜ | dsh 沙箱只管写不管读：#30–#32 的隔离目前只在提示词、网络工具与 API 层成立。写为接受的风险，接入第二个运行时或多机部署前必须解决 |
| 35 | 看板只展示用户下发的任务，Agent 之间的委托不上看板 | ✅ | 新增 UserRequest：群聊 @、私聊消息里有交付目标的事，由接任务的 Agent 判断后 `open_request` 登记（闲聊、问答不算；来源消息与下发人由网络认定），做完 `deliver_request`，下发人在看板上验收或退回（原会话返工）；也可在对话中明确表示通过，由接任务席位 `accept_request` 代验收。网络校验本轮实际送达且 @ 当前席位的下发人消息、任务归属与待验收状态、无运行中委托，记录 `reviewed_by` 与 `accepted_by_message_id`，事件注明验收人及代办 Agent。验收语义由模型判断，不开放 Agent 取消。为它发起的委托只在任务详情里展示。**限制**：是不是任务靠模型判断，可能漏登或误登；人目前不能手动把一条消息标为任务 |
| 36 | 弱网下偶发“@ 不到 Agent”“对话不存在或已删除” | ✅ | 前端把“请求失败”当成了“不存在”或“没有成员”。`api()` 的错误带状态码（断网 / 超时为 0，30 秒超时）；对话详情统一走 `loadConv`：只有 404 才显示“已删除”，其它失败保留上一次的数据、标出“网络不稳定”并退避重试；慢网下响应乱序时只认当前对话、更新的那次响应；打开新对话先显示“加载中”。@ 补全在对话详情暂缺时用群组成员兜底。事件流重连收到非 200（如网关 502）时浏览器会放弃，改为手动退避重连，网络恢复（`online`）时立即重连。**限制**：前端没有自动化测试，靠浏览器里注入失败 / 延迟验证 |
| 37 | 云侧与端侧运行时：用户本地 harness 接入，网络中同等看待 | 🟡 | 方向已定为**平台云侧（内置 dsh）+ 用户端侧（Claude Code / Codex 等）**，位置是节点属性，不改 Seat / Task / 审批 / 知识 / 群组语义。设计见 [docs/device-runtimes.md](docs/device-runtimes.md)。**已实现**：运行时能力报告与三项交集（#38）、连接器协议 `matrix/device.py`（握手与能力校验、`run.request` / `run.event` / `run.result`、工具与审批回执、超时取消）、`DeviceRuntime`、按 Agent 选运行时（`Agent.device_runtime`、`Network.register_runtime` / `runtime_for`）、端侧离线按传输错误处理。**已实现**：设备令牌（一个用户多条，只存 sha256，可单独吊销）与 `/api/devices/connect`（出站 WebSocket，令牌走 query，握手校验能力）。**已实现**：连接器本体 `matrix-connect`（`matrix/connector/`）：出站 WebSocket 与退避重连、本机桥把 `/internal/tools` 与 `/internal/approval` 转回网络、工作目录边界（拒绝绝对路径与 `..`）、dsh / Claude Code / Codex 三个驱动（Claude Code 用工具白名单，Codex 用 `-s read-only|workspace-write`，dsh 复用云侧覆盖层与沙箱）。已用本机 dsh、Codex、Claude Code 三个驱动跑通真实端到端（Claude Code 含 `--resume` 的第二轮）。**端侧其余部分已实现**：材料下发（`run.request.materials`）与 `publish_file` 回传（`file.fetch`）、设备管理页与Agent 运行位置选择、`TaskStatus.WAITING` 与重启 / 审批的端侧语义（#39）。**未实现**：云侧读隔离（#34） |
| 38 | 运行时能力报告与权限自动收窄 | 🟡 | `runtime.RuntimeCapabilities`（`enforced` / `advisory` / `unavailable`）+ `AgentRuntime.capabilities()`；`Network._effective` 把席位有效权限交运行时能力，委托授权再交执行者的能力（三项交集），做不到的记进 `Task.narrowed` 与委托结果，不算"未授予"。Agent 详情返回 `effective_authority` 与逐项等级。**现状**：只有 dsh 一个运行时（全部 `enforced`），`ScriptedRuntime` 可注入收窄的能力表用于测试；端侧适配器接入后才真正生效 |
| 39 | 端侧可达性：等待而不是失败；重启与审批语义区分位置 | ✅ | 任务新增 `WAITING`：端侧不在线时不调度、不判失败、不消耗重试，对话保持 `waiting`；端侧席位只给所有者写一条“已排队”说明；`Network.register_runtime` 上线即唤醒该设备的等待任务。`Store._load` 按 `delegatee.device_runtime` 区分重启语义（端侧回 `WAITING`）。端侧审批不按时间作废，只在设备断连时结束。离线期间沿用最近一次握手声明的能力（`Agent.enforcement`），避免误收窄 |
| 42 | 云侧 Agent 的预置模型，部署方灵活增减 | ✅ | `matrix/model_catalog.py`：部署方维护一份 JSON（默认 `<data>/models.json`，`--models` / `MATRIX_MODELS` 可改），列出 `{id, label, provider, model, reasoning_effort?}` 和可选的 `default`；按修改时间热加载，不用重启。启用后云侧 Agent 只能选目录里的模型（`Agent.model_preset`，旧客户端给的具体配置对得上就算选了它），不选用目录默认；下架的模型下一轮起改用默认，UI 标“原选择已下架”，重新上架自动恢复。文件写坏时沿用上一份（第一次就坏则只允许运行时默认），不因写错而放开限制；删掉文件回到自由填写。端侧 Agent 用自己 harness 里的模型，不受约束。`GET /api/models` 需登录 |
| 41 | 简化端侧接入：一机多 harness、设备码配对、后台服务 | ✅ | 见 [docs/device-runtimes.md](docs/device-runtimes.md) 2.4–2.6 节。一台电脑一个连接器，握手报告本机全部 harness（`hello.ok.runtimes`），Agent 绑定“设备 + harness”（`Agent.harness`），有效权限按绑定的 harness 计算，设备上没有那个 harness 时按不可达等待。`matrix-connect setup <服务器>` 走设备码配对（浏览器里批准，令牌经轮询只交付一次，申请数与输错次数有上限），配置存本机 0600 文件，再装成 launchd / systemd 用户服务（Windows 用任务计划程序）。设备页只给一条不含密钥的命令；手动签发令牌留作没有浏览器时的备用 |

## 六、项目

| # | 需求 | 状态 | 核心思路 |
|---|---|---|---|
| 40 | 项目维度：一件事下有不同方面的任务，由不同群组 / Agent 承载 | ↩️ 已被 #43 取代 | 原设计已由 #43 的单层项目替换（旧数据迁移见 [docs/projects.md](docs/projects.md) 第 5 节）。项目是目标与汇总层，不是超级群组：只汇总挂载群组里的用户任务，不流动上下文。**第一阶段已实现**：Project（一个所有者、brief、方面）、内部群组挂项目与方面、任务归属由网络按群组认定、私聊任务手动归类、项目页按方面分栏、看板按项目 / 方面筛选；原“项目引用”改名为资源引用（`refs`）。**第二阶段已实现**：项目背景（brief、方面与承担的群组）按版本推送给挂载群组的席位与任务，项目文件只读挂载到 `project/`，`publish_file target=project`、人上传 / 把群组文件提升到项目。**跨群组交接已实现**：`handoff` 由项目所有者确认后以人的名义发进目标群聊（不变量 19）；**负责人统筹**：所有者可以把交接确认、以及这些交接引出的任务的验收交给项目负责人 Agent，网络强制上限、越界转给人（不变量 20）。委托产物随交付拷进委托方工作目录（`deliveries/`），人可浏览 Agent 工作目录。**联合群组挂项目已实现**：只露共享部分（brief、项目决定、它的方面、共享给它的文件、它提交的交接），合作方提交的交接由负责人或人分派到内部群组（不变量 18、19） |
| 43 | 群组与项目收敛为“项目”；项目会话 | ✅ | 见 [docs/projects.md](docs/projects.md)。群组改名为项目，原来的项目层（方面、挂载、交接、负责人、共享）删除，委托边界就是项目，联合项目全可见，brief 改名“纲要”。每人指定一个助理 Agent（`User.assistant_id`，默认最早创建的），进项目时自动加入、人在项目里时不能移出；人在项目里开多个私密的项目会话和助理对话（不变量 18），带项目档案、有上限的现状快照与只读工具 `read_chat` / `read_request`（按所有者可见性过滤），能力同群聊席位，档案写入在群聊记事件。旧数据在 `Store._load` 里迁移，原样备份到 `state.v1.json`（`tests/test_migration.py`） |
| 44 | 本体不写死领域约束；运行上限归 Agent 设置 | ✅ | 任务提示词去掉偏调研的“800 字以内”“工具调用 6 次以内”“证据来源 URL”，改为通用的交付说明（结论、交付了什么、在哪里、怎么确认），篇幅与形式跟随委托的输出要求与约束。每轮运行超时（`Agent.timeout_s`）与委托次数上限（`Agent.max_delegations`）从网络常量挪到 Agent 设置，默认不限制，编辑页可设、留空即不限制；端侧随 `run.request` 下发超时，dsh / Claude Code / Codex 驱动各自执行。超时不再兜底后，端侧连接断开时等结果的轮次立刻按传输错误结束、任务回到等待。深度上限与退回、续接次数仍是网络常量 |
| 45 | 人主动终止一轮运行 | ✅ | 超时默认不限制后，卡住的一轮要能由人停下。对话里“运行中”的那条和委托详情各有“停止”按钮，只有正在跑的 Agent 的所有者看得到、能用（它用的是所有者的资源）。网络给这一轮发停止信号：云侧 dsh 结束进程，端侧发 `cancel`，dsh / Claude Code / Codex 驱动都会结束 harness（原来端侧收到 `cancel` 不处理）。这一轮按 `STOPPED` 处理，不自动续接：席位中断，点“继续”在同一会话接着做；任务失败，可续接，委托方的通知写明是人终止的、不要直接续接或重新委托 |
| 46 | 对话里发附件（群聊、项目会话、私聊） | ✅ | 参照 dsh 的附件（research: deepseek-harness#attachments）。输入框里的附件按钮、拖放与粘贴，选中即上传（`POST /api/conversations/<id>/attachments`，请求体就是文件，名称走 `?name=`，单个 ≤20 MB，一条消息 ≤10 个），传完的随下一条消息发出；消息上的附件卡片点了下载。按 sha256 只读存进 `<data>/attachments/<对话>/`，不在任何工作目录或私有目录里。席位每轮把它看得到的消息上的附件拷进工作目录的 `attachments/<sha 前 8 位>/<文件名>`（只读副本，运行后丢弃），提示词里每个文件只有一行路径，模型用自己的工具读；端侧随 `run.request.materials` 下发。委托方用 `delegate` 的 `attachments`（每行一个路径）显式转交给任务。上传、下载都按对话可见性，看不到一律 404（不变量 13）。清空 / 删除对话时删掉没有消息或委托引用的附件。不按文件类型区分。**限制**：图片也只是文件，模型那边的图片输入要换运行时（ACP）另做；读隔离同 #34 |
| 47 | 连接器与技能，个人和项目分开配置 | ✅ | 设计见 [docs/connectors-and-skills.md](docs/connectors-and-skills.md)，实现说明见 [docs/reference/connectors-skills.md](docs/reference/connectors-skills.md)。两个并列的用户概念（参照 WorkBuddy 的连接器，research: workbuddy#connector-model）：**连接器**是外部服务的接入，要授权、动的是授权人的状态，默认隔离；**技能**是做法，是能力，可以共享。个人层：我的连接器（云端授权或我电脑上 AI 工具里配好的服务，勾给哪些 Agent）、我的技能库（勾给哪些 Agent）；项目层：项目技能（档案的一部分，主持人写或批准 `propose_skill`）、需要的连接器（只声明，看成员就绪状态，不持有凭证）。自定义连接器的接入方式目前只有远程 MCP，配置手工写（`mcpServers` 写法），个人的可以再改；项目里也能写，请求头只放 `${变量}` 模板，成员连接时填自己的。云端连接器由网络凭执行上下文令牌代调用，凭证在加密保管里、不进运行时（不变量 19）；电脑上的连接器由驱动只开这一轮允许的（Claude Code `--strict-mcp-config`、Codex 关掉其余、dsh 禁用插件）。新权限 `connector.use`，委托不授予；联合项目里默认不开，所有者逐个打开。**顺带修掉**：云端 dsh 会读部署机器上的 `~/.dsh/skills`、`~/.agents/skills`（覆盖层现在总是关掉默认技能根）；Claude Code 驱动会加载用户全部 MCP 服务。**限制**：OAuth 授权未做（只有表单字段）；读隔离之前 `vault.key` 与运行时在同一台机器上（见 security.md） |
| 48 | 项目插件：自定义项目页面，接入业务场景，规划 workflow | ✅ | 设计见 [docs/project-plugins.md](docs/project-plugins.md)，实现说明见 [docs/reference/plugins.md](docs/reference/plugins.md)。插件包 = 清单声明的**表**（项目档案的一部分，记录按清单校验、进 `log.jsonl`）+ **页面**（一开始就支持自定义 HTML/JS，跑在不透明来源的沙箱 iframe 里，拿不到登录凭证、不能联网，读写只经父页面的桥、按看的人的权限）+ **技能**+ **触发**（外部回调 / 定时 / 人改了记录 → 往群聊发一条 @ 消息，代表启用它的人）。workflow 是“技能 + 触发”，不硬编排（纲领：不构建固定流程编排器）。来源：部署方目录 `plugins.json` 或主持人上传。新增不变量 20。示例：`examples/plugins/sales-pipeline`；插件作者指南 [docs/plugin-authoring.md](docs/plugin-authoring.md)，本地校验 `matrix-plugin check`。**限制**：回调没有 HMAC 签名、没有 `propose_plugin`；iframe 自己跳转到外部地址挡不住（见 security.md），所以只有主持人能装 |
| 49 | 默认云端助理：助理对话、跨项目总览、起草、定时与早报 | ✅ | 实现说明见 [docs/reference/assistant.md](docs/reference/assistant.md)。参照常驻个人 Agent 的共同做法（research: always-on-personal-agents#matrix-takeaways、openai-dots#responsibilities、claude-duet#proactivity）。注册即有一个云侧助理（没有 Agent 的人），不再引导创建第一个 Agent，登录后落在侧栏最上面的**助理**（和助理的私聊）。主页每轮推送跨项目总览（等你处理的事、各项目进行中的任务与最新动态，按你的可见性过滤；联合项目单列、标明是合作方写的），能读任一项目的群聊与用户任务；往项目里说话只能**起草**，你在确认卡上改、发，以你的名义带“助理起草”标记。**定时**只属于你、只唤醒你自己的 Agent，助理按你的话建，在“助理与定时”里统一看、停、删；默认工作日 09:00 早报，没有新情况不打扰；定时轮次不能登记用户任务。读到联合项目内容的那一轮不能调连接器（参照 Muse 的污点跟踪，research: meta-muse#sentinel）。新增不变量 21。**限制**：连接器写操作的逐次确认未做；一轮中途才读到联合项目时挡不住电脑上的连接器（见 security.md） |
| 50 | 去掉项目类型；信息只流向当时在场的人 | ✅ | 设计见 [docs/projects.md](docs/projects.md) 第 6 节。项目没有内部 / 联合之分：任何项目都能邀请，有参与者或待接受的邀请就是共享的（`Project.shared`，API 仍给推导出的 `kind`），第一次邀请时群聊席位换新会话。去掉私有目录挂载开关（`home_enabled`），改为网络给私有目录里每个文件记来源场合（`matrix/flow.py`、`<data>/flow/<agent>.json`），只挂技能和能给这里的人看的笔记，其余用 `open_note` 读；读了就记进会话来源，能联网 / 执行命令 / 用连接器的一轮里延到下一轮交出，之后这个会话没有这三种能力。群聊回复与委托交付的听众不全在来源场合里时拦下给所有者确认（发这一次 / 以后都能用 / 不用它重写），会让别人看到内容的网络工具拒绝。有别人的场合里技能只读。参照情境完整性与 CaMeL / Dual LLM（research: information-flow-control#contextual-integrity、research: information-flow-control#camel）。新增不变量 22。**限制**：读隔离之前挡不住主动绕路；端侧 Agent 第一版在共享项目里仍不挂私有目录 |
| 51 | 项目页分说明 / 资料 / 技能 / 连接器；连接器面板；共享项目里用到时先问 | ✅ | 实现说明见 [docs/reference/connectors-skills.md](docs/reference/connectors-skills.md)（连接器面板、共享项目里的授权）与 [docs/reference/archive.md](docs/reference/archive.md)，设计见 [docs/connectors-and-skills.md](docs/connectors-and-skills.md) 2.4。项目页只回答三个问题加一个面板：说明（纲要 + 已定的事）、资料（文件与链接合成一处，原来的“资源引用”去掉）、技能（项目技能），连接器面板汇总这个项目用到的服务，来源有四种——资料里的链接（连接器目录加 `domains` 按域名认，认不出的主持人指定）、插件清单的 `connectors` 声明、实际调用过的、主持人手动添加的（原来的“需要的连接器”），每行给成员就绪状态，不给账号。共享项目里的授权从“所有者逐个打开、默认关”改成三档（用到时问 / 这个项目里都可以 / 不行，默认用到时问）：第一次调用时停下给所有者一张卡片（只这一次 / 都可以 / 不行），不变量 19 改写。群聊旁的“档案”改成只读的“概览”。旧数据迁移：打开过的改成“都可以”，关联了资源引用的需要并进那条链接。**限制**：电脑上的服务网络拦不到调用，没有“用到时问”，只能选都可以或不行；域名认不出的要主持人指定 |
| 52 | 管理后台：观测使用情况 | ✅ | 实现说明见 [docs/reference/admin.md](docs/reference/admin.md)。每次运行时调用记一行用量台账（`<data>/usage.jsonl`：所有者、场合、云侧 / 端侧、模型、成败与错误类别、耗时、工具调用、token），不记内容；`/admin` 页面给部署方指定的管理员（`--admins` / `MATRIX_ADMINS`，默认最早注册的人）看总览、按天走势、按用户 / 模型 / 运行位置 / 场合 / 失败原因的分组。只看用量不看内容：统计里不出现项目名、Agent 名、消息正文 |
| 53 | 在管理后台配置云侧可用的模型 | ✅ | 实现说明见 [docs/reference/admin.md](docs/reference/admin.md)“模型目录”。后台的“模型”页编辑的就是 #42 的 `models.json`：增删改、选默认，整份校验、带版本保存（期间被别处改过就拒绝）；添加第一个模型即启用目录；列出在用但不在目录里的模型和 Agent 数，可一键加进目录。保存后打开着的页面自动刷新模型列表，用户创建云侧 Agent 只能从中选 |
| 54 | 在管理后台配置不同供应商的模型（协议、地址、密钥） | ✅ | 参照 dsh 的 `llm-pi-ai`（research: deepseek-harness#model-providers）。“模型”页下方的供应商：名称、协议（OpenAI Chat Completions / Responses、Anthropic Messages）、地址、密钥；模型可以选平台的供应商（再填上下文长度、最多输出、能否看图），也可以继续用 dsh 里已配置的。密钥加密存在 Matrix（`vault/`），只写不读，只在云侧用到它的那一轮放进那个 dsh 进程的环境变量；覆盖层只写这一个供应商与变量名。`matrix-dsh-check` 用假服务核对供应商生效、Agent 的命令看不到密钥。**限制**：读隔离之前 Agent 能读 `vault/` 与保管密钥（同连接器凭证）；端侧 Agent 不能用平台的供应商 |
| 55 | 项目对外：发布与订阅；交付看板 | ✅ | 设计见 [docs/project-publishing.md](docs/project-publishing.md)，实现说明见 [docs/reference/releases.md](docs/reference/releases.md)，调研 research: project-publishing#three-faces。项目分作业面、发布面、节律面；项目内容离开项目只经**发布物**：主持人发布、按版本固定成快照，**技能包**（技能）可以公开，**交付件**（档案文件，可带插件看板）只给点名的读者，可跟随档案自动切版本，撤回即失效。读者在“发布物”里看、给自己的 Agent **订阅**：技能包进技能层，交付件只读挂到 `releases/`，只在这一轮能看到输出的人都是读者时生效，用订阅者自己的授权，发布方看不到对话；读者反馈以事件回到项目群聊。插件清单加 `release_views`，同一页面在发布物里只读快照。新增不变量 23。示例：`examples/delivery-dashboard`（演示交付数据 MCP、交付看板插件、`setup_demo.py`）。**限制**：共享项目里只要主持人同意；节律面（职责清单、`ask_decision`、整理回合）与发布物目录的排序未做 |
| 56 | 项目接入已有系统（看板系统）；连接器写操作逐次确认；Agent 把链接加进资料 | ✅ | 连接器只认 MCP（业界标准）：已有系统的 HTTP API 由部署方包成远程 MCP 服务登记进目录，地址不再写死在技能里，Agent 不需要 `shell.exec`。网络保留 MCP 工具标注（annotations）：部署方目录里不是只读、并且明确标了 `destructiveHint` / `openWorldHint` 的工具，**每次调用**都先给连接的授权人一张确认卡（照原样列出要发出的参数，只放行这一次，`Approval.kind = connector_write`），不变量 19 补充。新网络工具 `add_link`（档案工具）：Agent 把外部链接加进项目资料，同一地址再加是更新，连接器面板按域名认出。示例：`examples/dashboard-system`（公开看板系统 + MCP 包装层：建草稿 → 链接进资料 → 说发布 → 确认卡 → 发布）。**限制**：只认明确的标注（没标注的照旧直接调用）；电脑上的连接器网络拦不到。**外部页面标签**：部署方在连接器目录里允许嵌入（`embed`）的域名，主持人可以把资料里的链接设成项目页标签，直接嵌入那个系统的页面（看的人用自己的登录，Matrix 不读内容、不给桥）；前提是那个系统允许被嵌入 |
