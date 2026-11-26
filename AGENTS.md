# AGENTS.md

给在本仓库工作的 AI Agent 的项目说明与工程约束。本文件是入口：动手前读完它，再按“按需阅读”读与改动相关的文档。

项目方向与设计理念见 [CHARTER.md](CHARTER.md)（行动纲领原文）。本文件讲"当前代码怎么做"，
CHARTER 讲"我们要往哪走"。需要判断某个改动是否该做时，以 CHARTER 的方向为准。

竞品与相关项目的调研不在本仓库：它们在一个私有调研仓库里，检出在本仓库**旁边**（`../matrix-research`，
没有就跳过）。本仓库里写“参照 X”的地方带一个引用 `research: <笔记>#<锚点>`，指向那里的 `products/<笔记>.md`
或 `topics/<笔记>.md` 中的 `<a id="锚点">`。要了解某个设计从哪里学来、当时为什么这么定，按引用去读；
调研仓库在手边时，动相关设计之前先读对应笔记。**调研内容不要抄进本仓库**（本仓库公开），只写结论与引用。

## 项目描述

**Matrix**（代码包名 `matrix`）：一个本地运行的 Agent Network（Agent 协作网络）Demo。人在项目群聊里 @ Agent 交代任务（或在项目会话里和自己的助理单独聊、和单个 Agent 私聊）；被 @ 的 Agent 自己做，或通过网络工具（MCP）把子任务委托给同一项目的成员，网络负责授权、隔离、追踪与知识沉淀。

- 语言 / 依赖：Python ≥ 3.14，FastAPI + uvicorn，包管理用 `uv`，测试用 pytest。
- 运行时：默认以 DeepSeek Harness（DSH）headless 为 Agent 运行时；`scripted` 运行时是确定性脚本，用于测试与离线演练。
- 状态：全部持久化在 `--data` 目录（默认 `.matrix/`，只有改名前的 `.agentnet/` 时沿用它；都已被 gitignore）的 `state.json`；对话、任务各有独立工作目录 `conversations/<id>/`、`tasks/<id>/`，云侧每个执行单元（席位、委托）另有自己的 dsh 状态目录 `dsh-state/`（那一轮 dsh 的 HOME，不在工作目录里）；打开沙箱方式（`MATRIX_CLOUD_SANDBOX`，默认关）时执行单元的工作目录与状态在执行面目录树 `exec/` 里，产物与会话的检查点在 `checkpoints/` + `content/`，dsh 事件在 `run-logs/`；每个 Agent 有持久私有目录 `agents/<id>/`；每个项目有档案目录 `projects/<id>/`（`files/` 与变更日志 `log.jsonl`），不放进任何代码仓库。

核心概念（定义见 [domain.md](docs/reference/domain.md)）：

- **Agent**：网络里的节点，只属于、只为它的所有者工作；有自身权限上限，可跑在云侧或用户端侧。每个人有一个**助理**（自己的一个 Agent，注册即有），不会自己进项目，要它在就把它加进项目；它在项目里时，项目会话里没 @ 任何人的消息由它回答。
- **助理对话**：侧栏最上面的“助理”，和自己助理的私聊。助理在这里看得到你各个项目的进展，替你起草要发进项目的消息（你确认才发），按你的话定时提醒（默认工作日早报）。
- **公共 Agent**：长期负责一个系统（报销、请假、工单）、给所有人用的 Agent：维护组（所有者 + 维护人）申请、管理员批准。所有用户都能私聊它、把它拉进项目；它在**服务面**只为当时的调用方做事（私有目录只读不写回、用调用方自己的授权、过程只给调用方），维护人在**维护面**（自己和它的私聊）整理公共资料、只看服务面的用量。
- **项目**：成员集合与委托边界，没有主 Agent，也没有类型：邀请了别人就是共享的，这是跨用户协作的唯一途径。
- **对话**：每个项目一个群聊（被 @ 的成员接任务）；人在项目里可以开多个**项目会话**：自己在项目里的私下对话，带项目上下文，@ 项目里的任何 Agent（不需要助理）；每个 Agent 一个私聊。**席位**是 Agent 在一个对话里的执行上下文。
- **委托（Task）**：Agent ↔ Agent 的一次授权，异步执行、收件箱唤醒，由委托方验收。
- **用户任务（UserRequest）**：人交代的有交付目标的事，看板上只有它。
- **项目档案**：说明（纲要、已定的事）、资料（文件、链接）、项目技能，归项目所有，按版本推送给成员。
  项目页另有**连接器面板**：这个项目用到了哪些外部服务、每个成员能不能用，项目从不持有谁的授权。
- **插件**：项目装上的扩展（表、沙箱页面、技能、触发）；还可以带一个**插件服务**（标准 MCP：网络托管——主持人自己装的只在沙箱里——或远程运行；
  主持人可以从部署方目录装、上传、填地址装，Agent 只能提议），
  经挂点影响项目里的运行：工具、只读挂载、提示词里标明来源的一段、验收后的通知（如示例「项目 Wiki」），以及建议群聊消息交给谁
  （route，如示例「队友协作（Raft 式）」：插件只建议，网络按预算与规则执行）。不硬编排流程。
- **模板与对外页面**：做法给别人用走**模板**（原叫蓝图，代码里是 `blueprint`；项目里的角色原叫岗位，代码里是 `roles`。模板是项目去掉数据、人和凭证后的配方；别人勾选部件、建新项目或装进已有项目，拿走的是拷贝；
  技能包 2026-10-31 并进了模板）。结果给别人看走**对外页面**（原交付件，代码里是 `Release(kind="fact")`：档案文件的快照，可带插件页面，
  只给点名的读者，读者在“项目”的“给你看的页面”里看，给自己的 Agent 订阅、用自己的授权，发布方看不到对话）。

## 按需阅读

| 要改 / 要了解 | 先读 |
| --- | --- |
| 模型字段、状态结构、权限词表 | [docs/reference/domain.md](docs/reference/domain.md) |
| 群聊、项目会话、私聊、@ 唤醒、成员变动、共享项目与可见性、附件与 Agent 发文件（`send_file`） | [docs/reference/conversations.md](docs/reference/conversations.md) |
| 委托、收件箱、失败与续接、看板与用户任务、运行中提问（`ask_user_question`）、长期目标续跑与后台任务 | [docs/reference/execution.md](docs/reference/execution.md) |
| 项目档案与档案工具、资料的目录层级、资料与技能的可见范围 | [docs/reference/archive.md](docs/reference/archive.md) |
| 外部平台 Agent：平台目录、驱动、远程运行时、`/mcp/remote`（密钥 + 会话 id、先等再绑）、调用方授权透传 | [docs/remote-agents.md](docs/remote-agents.md)；接公司内部平台（确认清单、目录、写驱动、本地测、验收）见 [docs/remote-agent-authoring.md](docs/remote-agent-authoring.md)，样板 `examples/remote-agent/`（改了驱动接口要同步改这两处），不变量 28 |
| 公共 Agent：维护组、管理员批准、服务面与维护面、调用方（`principal`）、用量 | [docs/public-agents.md](docs/public-agents.md)（第 10 节是第一版的实现说明），不变量 27 |
| 项目、助理、项目会话、旧数据迁移 | [docs/reference/projects.md](docs/reference/projects.md)，设计见 [docs/projects.md](docs/projects.md) |
| 助理对话：跨项目总览、起草、定时与早报 | [docs/reference/assistant.md](docs/reference/assistant.md) |
| IM 入口（在企业微信等 IM 里和助理单聊）、IM 驱动、绑定 | [docs/reference/im.md](docs/reference/im.md)；接企业内部 IM（确认清单、连接方式、写驱动、本地测、上线、真机验收）见 [docs/im-driver-authoring.md](docs/im-driver-authoring.md)，样板 `examples/im-driver/`（改了驱动接口要同步改这两处） |
| 对外页面（发布物，原交付件；技能包已并进模板）：读者、版本与跟随档案、订阅生效条件、插件视图、反馈 | [docs/reference/releases.md](docs/reference/releases.md)，设计见 [docs/project-publishing.md](docs/project-publishing.md)，示例 `examples/delivery-dashboard/` |
| 模板：对外交付的唯一形式，项目去掉数据、人和凭证后的配方；建新项目或装进已有项目，部件可勾选（技能包并进来）；项目角色与插件设置；对外页面（原交付件） | [docs/blueprints.md](docs/blueprints.md)（第 16 节是第一版的实现说明），示例 `examples/blueprints/` |
| 项目插件（表、沙箱页面与桥、技能、触发；插件服务与挂点；自己装、沙箱、后台目录与上架申请） | [docs/reference/plugins.md](docs/reference/plugins.md)，设计见 [docs/project-plugins.md](docs/project-plugins.md)、[docs/plugin-hooks.md](docs/plugin-hooks.md)、[docs/plugin-install.md](docs/plugin-install.md)，示例 `examples/llm-wiki/`，给插件作者的指南见 [docs/plugin-authoring.md](docs/plugin-authoring.md)（改了清单字段、SDK、触发行为要同步改它） |
| 连接器（外部服务）与技能，个人与项目两层，项目的连接器面板与共享项目里的授权，写操作逐次确认，外部页面标签 | [docs/reference/connectors-skills.md](docs/reference/connectors-skills.md)，设计见 [docs/connectors-and-skills.md](docs/connectors-and-skills.md) |
| 把已有系统接进来：包成 MCP、工具标注与逐次确认、`connectors.json` 字段、外部页面标签对对方系统的要求、上线前检查清单 | [docs/connector-authoring.md](docs/connector-authoring.md)（部署方接入指南，示例 `examples/dashboard-system/`；改了目录字段、确认规则、嵌入限制要同步改它） |
| 端侧运行时、Matrix 客户端（`matrix-connect`）、设备配对、harness、材料下发（清单 + 缓存）、**云侧与端侧的差异与云侧的隔离欠账**、云侧外层沙箱（`MATRIX_CLOUD_ISOLATION`）与执行单元的 dsh 状态目录 | [docs/reference/device-runtimes.md](docs/reference/device-runtimes.md)，设计见 [docs/device-runtimes.md](docs/device-runtimes.md) |
| 业务系统自己的设备端 Agent 接入：配对时一键创建 Agent（`setup --agent`）、一次性配对令牌（`/api/devices/enroll`、`setup --code`）、外部驱动（entry point `matrix.connect_drivers`、`MATRIX_HARNESSES`）、把驱动打进独立程序 | [docs/device-driver-authoring.md](docs/device-driver-authoring.md)，样板 `examples/device-driver/`（改了驱动接口、加载约定、打包方式要同步改这两处） |
| 云侧执行面的生产级方案（阶段 0 单机止血已实现；阶段 1a 沙箱方式已实现、默认关（`MATRIX_CLOUD_SANDBOX`）；其余是设计）：每个执行单元一个沙箱、沙箱即托管设备、出口网关、分阶段与验收检查 | [docs/cloud-runtime.md](docs/cloud-runtime.md)；验证工具包 [deploy/isolation-check/](deploy/isolation-check/README.md) |
| 存储：`state.json` 怎么写（fsync、`.bak`、读不了时退到上一份）、数据库存储（`MATRIX_STORE`：SQLite / PostgreSQL，增量保存、消息与事件与轮次分表、首次启动自动导入、`export-state` 回退）、附件进内容存储（`MATRIX_BLOBS`：本机 / S3，全站去重、双读、配额）、保存耗时与等锁时间的指标（`/api/admin/storage`、`/metrics`） | [docs/reference/storage.md](docs/reference/storage.md) |
| 控制面的生产级方案（设计，未实现）：状态进数据库、重启不打断运行、备机接管、按租户分片、登录加固 | [docs/control-plane.md](docs/control-plane.md) |
| 任何会改变引擎行为的改动 | [docs/reference/invariants.md](docs/reference/invariants.md) 里相关条目的全文 |
| 用公司账号登录（单点登录）：`login.json`、内置 OAuth2 驱动、登录驱动接口、身份怎么对到用户；外部系统带着用户凭据直接调 `/api/*`（`api_auth`、`verify`，第八节） | [docs/login-driver-authoring.md](docs/login-driver-authoring.md)，样板 `examples/login-driver/`（改了驱动接口、`login.json` 字段要同步改这两处） |
| 登录、`/internal/*`、沙箱、文件下载、部署 | [docs/reference/security.md](docs/reference/security.md)；部署见 [deploy/README.md](deploy/README.md)（只管 Matrix 一个容器），对外暴露（反向代理的要求、Caddy 示例）见 [docs/reverse-proxy.md](docs/reverse-proxy.md) |
| 管理后台：用量台账（只看用量不看内容）、云侧模型目录、给用户发重置密码链接 | [docs/reference/admin.md](docs/reference/admin.md) |
| 前端样式：颜色、字号、间距、圆角、通用部件，插件页面的样式 | [docs/design-system.md](docs/design-system.md)，变量在 `matrix/static/tokens.css`，样式指南页 `/static/ui.html`；`tests/test_design.py` 检查迁移过的文件不写死颜色 |
| 需求与实现状态 | [REQUIREMENTS.md](REQUIREMENTS.md) |

## 模块地图

| 文件 | 职责 |
| --- | --- |
| `matrix/engine.py` | `Network` 核心：Agent/项目/对话管理、项目的参与者与可见性、项目档案、发布与订阅（发布物、版本快照、订阅挂载、发布物里的插件视图）、助理与项目会话、助理对话（跨项目总览、起草、个人定时）、@ 解析、收件箱调度、席位与任务轮次、委托授权、验收与续接、提议审核、私有目录挂载、信息流（来源标签、`open_note`、拦截确认）、知识可见性、违规检测、项目插件（安装、记录、桥、触发） |
| `matrix/protocol.py` | 对话 / 任务 / 通知提示词、交付与失败说明、`TOOL_GUIDE`（网络工具用法） |
| `matrix/runtime.py` | `AgentRuntime` 抽象；`DshRuntime`（`--patch` 覆盖层按权限禁用插件 + 沙箱 + 挂载 MCP + 按 Agent 设模型；云侧 `hosted`：执行单元自己的 dsh 状态目录当 HOME、外层 bwrap 读隔离（`outer_argv`，开关 `MATRIX_CLOUD_ISOLATION`）、不插审批应答者，旧会话迁移 `adopt_dsh_session`）；`ScriptedRuntime`；运行时错误分类 |
| `matrix/mcp_server.py` | 仅标准库的 stdio MCP 服务，`TOOLS` 是网络工具 schema 的唯一定义，回调 `/internal/tools` |
| `matrix/server.py` | FastAPI：登录、REST（按用户过滤）、看板 `/api/board`、SSE 事件流 `/api/events`、静态页面、运行时回调 `/internal/tools` 与 `/internal/approval` |
| `matrix/auth.py` | 用户注册 / 登录 / 退出（scrypt，仅标准库；登录令牌只存 sha256、闲置 7 天 / 最长 30 天过期）；第一个注册的用户接管已有数据；管理员签发的一次性重置密码链接；公司账号对到 Matrix 用户（`sso_login`：按入口 + 标识绑定） |
| `matrix/login/` | 用公司账号登录：`base.py` 驱动接口（`authorize_url` / `identify`，可选 `verify`：外部系统转来的用户凭据）、`registry.py` 内置驱动 + entry point `matrix.login_drivers`、`service.py`（部署方的 `login.json`，热加载；进行中的登录：只认发起的浏览器、核对 state、一次性）、`oauth2.py` 通用 OAuth2 授权码驱动、`testing.py` 驱动一致性检查。接口在 `server.py` 的 `/api/auth/sso/{入口}/start` 与 `/callback`；带着用户凭据的请求由 `server._api_caller` 认（`LoginService.verify`，按凭据哈希短暂缓存） |
| `matrix/dsh_plugins/approval.mjs` | dsh 审批应答者（只给端侧驱动；云侧不审批）：沙箱升权申请转给网络，由 Agent 所有者决定 |
| `matrix/dsh_plugins/jobs.mjs` | dsh 后台任务守护：Agent 要收尾时还有后台任务在跑，追加一条消息让它先收完或结束（headless 退出会结束它们） |
| `matrix/dsh_plugins/questions.mjs` | dsh 提问应答者：`ask_user_question` 的问题转给网络（席位里问人，委托里先问委托方），阻塞到有回答；没回答时报错，不当成同意 |
| `matrix/sandboxes.py` | 云侧执行面的沙箱方式（阶段 1a，开关 `MATRIX_CLOUD_SANDBOX`，默认关）：`SandboxProvider` 接口与 `SandboxSpec`（只有执行单元 id、所有者、Agent、代数、配额、一次性启动令牌）、`MemorySandboxes`（测试用假后端）、`LocalSandboxes`（本机子进程 + 外层 bwrap，工作目录 bind 到原来的绝对路径）、`SandboxHub`（启动令牌换执行单元凭证，一条连接只服务一个单元）、`SandboxRuntime`；引擎里的台账、对账、容量、材料与检查点在 `engine.py`“云侧执行面：沙箱方式”一节 |
| `matrix/sandboxd.py` | 沙箱管理器 `matrix-sandboxd`（阶段 1b 的 `docker` 后端）：唯一持有 docker.sock 的进程，Unix socket 上的本地 HTTP；请求校验（只认执行单元 id、所有者、Agent、代数、配额、启动令牌、允许前缀下的逻辑工作目录，`validate_ensure`）、`docker run` 参数（`run_argv`，纯函数：非 root、去掉全部特权、只读根、配额、只挂这个单元的两个卷）、运行时选择与失败关闭（`runsc` / `insecure-runc`）、标签与列举；控制面这边的客户端是 `sandboxes.DockerSandboxes` |
| `matrix/gateway.py` | 出口网关（阶段 2a）：核心进程里的模块、单独的监听；只转沙箱连接这一个端点、模型代理（令牌认定这一轮，核对供应商与模型，换真密钥，流式透传，记用量）、出网代理（`decide` 纯函数：权限 × 主机 × 地址，连接固定在校验过的地址上；2b 解开 HTTPS 后 `check_request` 按方法、URL 与出网预算逐个请求放行）、每个请求留痕 |
| `matrix/egress_ca.py` | 出网 CA（阶段 2b）：私钥只在控制面，按主机现签叶子证书，网关据此解开沙箱的 HTTPS |
| `matrix/migrate.py` | `matrix migrate`：给现有工作目录建检查点（`workdirs`）、回退写回原目录（`export`）、清理原目录与旧 dsh 状态（`purge-legacy`，要 `--yes`） |
| `matrix/checkpoints.py` | 内容存储（按 sha 的本机目录，接口留出换对象存储的余地）、分块收沙箱交来的内容并校验、每个执行单元的检查点（产物清单 + 最新两份 dsh 会话包）、旧工作目录导入、会话打包与解包 |
| `matrix/connector/` | 端侧连接器 `matrix-connect`：出站 WebSocket、本机桥（`/internal/*` 转发出站）、dsh / Claude Code / Codex 驱动与外部驱动（entry point `matrix.connect_drivers`；一个连接器同时提供多个）、设备码配对与一次性配对令牌（`pairing.py`）、本机配置（`config.py`）、后台服务（`service.py`）；云侧沙箱客户端（`sandbox.py`，`Connector` 的薄子类：一个沙箱一个执行单元、私有目录“材料进、改动出”、交检查点与恢复） |
| `matrix/connect_install/`、`packaging/` | 连接器的免依赖安装：服务器在 `/connect/install.sh`、`/connect/install.ps1` 提供的安装脚本，PyInstaller 配方（CI：`.github/workflows/connect-release.yml`）；设计见 docs/device-runtimes.md 第 2.7 节 |
| `matrix/im/` | IM 入口：`base.py` 驱动接口（字段、能力、收发）、`registry.py` 内置驱动 + entry point `matrix.im_drivers`、`bridge.py` 桥（绑定码、只认绑定的人、回复回到 IM、推送、按能力降级、限频，不变量 25 都在这里）、`wecom.py` 企业微信长连接驱动、`fake.py` 测试用的假驱动、`testing.py` 驱动一致性检查 |
| `matrix/remote_agents.py` | 外部平台 Agent（不变量 28）：平台目录 `PlatformCatalog`（`agent-platforms.json`，热加载）、驱动接口与内置标准 HTTP 驱动（entry point `matrix.agent_platforms`）、`RemoteRuntime`（读流、转事件、中断）、`Bindings`（平台会话 id → 这一轮的网络工具，带等待）；引擎里的添加、凭证、调用方透传与 `remote_mcp` 在 `engine.py`「外部平台 Agent」一节 |
| `matrix/mcp_sse.py` | `/mcp/remote` 的旧版 MCP 传输（HTTP+SSE）：`GET /mcp/remote` 开流、`POST /mcp/remote/messages` 发消息，规则同 `remote_mcp`（不变量 28） |
| `matrix/skills.py` | 技能文件（`SKILL.md` + 附带文件）的读写校验、按层合并（项目 > 连接器自带 > 我的）与挂载 |
| `matrix/plugin_service.py` | 插件服务（不变量 24）的客户端：网络托管的 stdio MCP 进程（`ServiceHost` 按项目 × 插件一个，按需启动、空闲停掉、版本换了重启、连续起不来暂停；最小环境 `service_env`；按带哈希的锁文件建依赖环境 `build_env`）与远程 MCP（`RemoteService`，每次调用带 `sign_token` 签的项目令牌；目录里的用部署方的密钥，自己装的每个安装一把）；工具、资源都走标准 MCP。工具按原名 `<插件>__<工具>` 经 `mcp_server.py` 的 `__list_tools__` 动态列出；挂点与工具在 `engine.py`「插件服务」一节 |
| `matrix/blueprints.py` | 模板（[docs/blueprints.md](docs/blueprints.md)）：格式与校验、`{param:…}` / `{role:…}` 占位符、发布时的可移植检查、`BlueprintStore`（`<data>/blueprints/<id>/v<N>/`）；发布、可见性、用模板建项目在 `engine.py`「模板」一节 |
| `matrix/plugin_sources.py` | 插件从哪里来（[docs/plugin-install.md](docs/plugin-install.md)）：部署方的安装策略（`plugin-policy.json`）、按地址取包（`fetch_package`，复用 `web_fetch` 的地址校验）、确认卡条目（`disclosure`）、后台目录（`ManagedCatalog`，git 浅克隆 / 上传）、上架申请（`Submissions`）、合起来的目录（`CombinedCatalog`） |
| `matrix/sandbox.py` | 插件服务的沙箱（不变量 24）与云侧 dsh 的外层沙箱（`nested`：里面再起 dsh 自己的 bwrap）：Linux bubblewrap 包装命令（新命名空间、默认不联网、只挂必要的目录）、rlimit、可用性探测（`available("plugin" / "nested")`） |
| `matrix/plugins.py` | 项目插件：清单（`plugin.json`）与包的校验、安装、`page/` 取文件、记录的存取与按清单校验、cron；部署方插件目录 `plugins.json`（`--plugins` / `MATRIX_PLUGINS`，热加载）。引擎里的安装、桥、触发在 `engine.py`「项目插件」一节 |
| `matrix/releases.py` | 发布物的版本快照（`releases/<id>/v<N>/`：技能、文件、插件页面、`manifest.json`）的写入、清理与安全取文件；谁能发布、能看、订阅何时生效在 `engine.py`「发布与订阅」一节 |
| `matrix/plugin_check.py` | `matrix-plugin check`：本地校验插件包（错误用安装时同一套校验，另给启发式提醒：桥客户端路径、外部资源与联网、没申请的能力、触发消息里会变空的字段） |
| `matrix/dsh_check.py` | `matrix-dsh-check`：对真实 dsh 的冒烟检查，升级云端 dsh 前后用（静态：覆盖层引用的插件都在、新工具插件提醒、覆盖层能加载、配了抓取白名单时自带的 web_fetch 关得掉；假的模型服务核对平台供应商生效、Agent 的命令看不到密钥、在 bwrap 沙箱里（不在就是 dsh 退到了更弱的沙箱）、云侧外层的读隔离与进程隔离（`read-isolation`、`proc-isolation`）；`--live`：回复、续接、技能、委托、连接器、工作目录以外写不进且不审批、提问） |
| `matrix/static/plugin-sdk.js` | 插件页面里的桥客户端（`window.matrix`），经 `postMessage` 让父页面转给网络 |
| `matrix/connector_catalog.py` | 云端连接器目录：部署方维护的 `connectors.json`（`--connectors` / `MATRIX_CONNECTORS`），热加载；地址只来自这里 |
| `matrix/vault.py` | 连接器凭证与模型供应商密钥的加密保管（Fernet，`MATRIX_VAULT_KEY` 或 `<data>/vault.key`） |
| `matrix/mcp_client.py` | 网络这一侧的远程 MCP 客户端（Streamable HTTP，JSON / SSE），代调用云端连接器 |
| `matrix/mcp_connectors.py` | 运行时这一侧的 stdio 桥：把连接器工具转发到 `/internal/connectors/*`，只带执行上下文令牌 |
| `matrix/web_fetch.py` | 带白名单的网页抓取（仅标准库的 stdio MCP，`mcp__matrix_web__fetch`）：部署方的 `fetch.json`（`--fetch` / `MATRIX_FETCH`，热加载）在时，云侧有 `web.research` 的 Agent 用它替代 dsh 自带的 `web_fetch`（覆盖层关掉自带的、保留 `web_search`）；公网照抓，内网只抓白名单里的域名与网段，连接固定在校验过的地址上，每一跳重定向重新校验 |
| `matrix/model_catalog.py` | 云侧预置模型目录：部署方维护的 `models.json`（管理后台的“模型”页也编辑它，带版本整份保存）（默认 `<data>/models.json`，`--models` / `MATRIX_MODELS`），按修改时间热加载；启用后云侧 Agent 只能选目录里的模型，下架的改用默认（`Network.model_info` / `run_model`）；端侧不受约束；没有文件时照旧自由填写。也存平台配的模型供应商（协议、地址；密钥在 vault），用到时覆盖层只写那一个供应商（`llm_config` / `Network._llm_provider`） |
| `matrix/workdir.py` | 工作目录里的“产物”口径：委托产物快照（`deliveries/`）、目录打包发布、人浏览工作目录 |
| `matrix/homedir.py` | 私有目录的挂载与三方合并写回，云侧（engine）与端侧（connector）共用 |
| `matrix/flow.py` | 信息流的判断（不变量 22）：私有目录文件的来源标签、在场的人、挂载与放行，纯函数集合比较，不用模型 |
| `matrix/db.py` | 数据库存储的 SQL 薄层（SQLite / PostgreSQL 一套表：`entity`、`message`、`event`、`run`、`meta`）、`EventLog`（事件成组落库）、`resolve_url`（`MATRIX_STORE` 的写法） |
| `matrix/blobs.py` | 附件的内容存储（M8）：`LocalBlobs`（`<data>/blobs/ab/cd/<sha>`）、`S3Blobs`（SigV4，读缓存按 sha 校验）、`Staging`（暂存区与清理）、`from_config`（`MATRIX_BLOBS`）；“哪个对话上传过哪份”在 `Store.attachment_refs`，读写在 `engine.py`「对话附件」一节 |
| `matrix/storage_cli.py` | 存储迁移命令：`matrix migrate blobs`（附件搬进内容存储 / `--to-legacy` / `--purge-legacy`）、`matrix migrate state`（导入 + 对账）、`matrix migrate verify`（按内容比对）、`matrix export-state`（回退）、`matrix backup-db` |
| `matrix/store.py` | 持久化（`write_durable`：fsync + 留 `.bak`；`TimedRLock` 与 `Timings`：等锁与保存耗时的指标；`JsonBackend` 整份写 state.json / `DbBackend` 只写变了的行，`ENTITY_KINDS` 是入库的状态清单）、旧数据迁移（`_merge_groups`：群组 + 项目两层合成一层，`_one_chat_each`、`_one_trust_domain`）与种子数据（5 个种子 Agent + 一个研究项目；服务默认不预置，CLI 与测试预置） |
| `matrix/compat.py` | 改名兼容（代码原名 agentnet）：部署方环境变量 `MATRIX_*` 没设时读 `AGENTNET_*`（`env`）、默认数据目录沿用旧的 `.agentnet`（`data_dir`）、旧网络工具名换成 `mcp__matrix_net__*`（`current_tool`，用于旧 state.json 与没升级的连接器）。改名相关的兼容都放这里 |
| `matrix/usage.py` | 用量台账（`<data>/usage.jsonl`，每次运行时调用一行，不记内容）与管理后台的统计（纯函数）；引擎里的管理员认定与汇总在 `engine.py`「管理后台」一节 |
| `matrix/events.py` | 进程内事件总线（带 seq，供 SSE 与 CLI 消费） |
| `matrix/scripted.py` | 脚本运行时的场景处理器 |
| `matrix/cli.py` | 无 UI 跑一轮对话并打印事件流 |
| `examples/im-driver/` | IM 驱动样板：虚构的“演示 IM”（能改消息、不能发卡片）的驱动包（entry point `matrix.im_drivers`）、能在终端里扮演 IM 用户的本地服务 `devserver.py`、驱动自己的测试（在默认 `testpaths` 里）；企业内部 IM 照它改 |
| `examples/login-driver/` | 登录驱动样板：虚构的票据式“演示 SSO”的驱动包（entry point `matrix.login_drivers`）、能在浏览器里点的假身份系统 `devserver.py`、配它的 `login.json`、驱动自己的测试（在默认 `testpaths` 里）；公司的统一登录不是 OAuth2 时照它改 |
| `examples/remote-agent/` | 外部平台 Agent 样板：虚构的“演示平台”`demo_platform.py`（标准 HTTP 形状、流开头带会话 id、Agent 级 MCP 回调、中断）与平台目录 `agent-platforms.json`；接公司内部平台照它改 |
| `examples/dashboard-system/` | 接入已有看板系统的示例：公开的看板系统 HTTP API（`board_api.py`）包成标准远程 MCP（`board_mcp.py`，带工具标注），部署方目录登记；Agent 建草稿、`add_link` 进资料、发布前所有者确认；`setup_demo.py` |
| `examples/teammates/` | 队友协作（Raft 式）演示：前端、后端、测试三个队友，被动投递、任务板认领、略过不发言、人说“#N 完成”，`setup_demo.py`（加 `--prepare` 只搭项目），见该目录 README |
| `examples/llm-wiki/` | 项目 Wiki 演示：两份演示资料（`materials/`）与 `setup_demo.py`（装插件、建知识管家、放资料），见该目录 README |
| `examples/delivery-dashboard/` | 交付看板示例（发布与订阅的验证场景）：演示交付数据 MCP 服务、部署方连接器与插件目录、交付看板插件（`release_views`）、`setup_demo.py` 搭演示数据 |
| `examples/plugins/` | 示例插件：销售漏斗（表、看板页面、技能、三种触发）；队友协作（`teammates`，Raft 式：route 被动投递、任务板与认领锁、任务板页面）；项目 Wiki（`llm-wiki`：插件服务 `service/wiki_engine.py`（stdio 托管或 `--http` 远程，带 MCP 资源）、三种挂点、技能 wiki-ingest / wiki-query / wiki-lint、Wiki 页面）；`examples/plugins.json` 是对应的部署方目录 |
| `matrix/static/` | 单页前端（原生 JS）；`tokens.css` 是设计变量的唯一来源（主界面、管理后台、插件页面经 `../_matrix/tokens.css` 共用），`ui.html` 是样式指南；`about.html` 是产品介绍页（`/about`），含可展开的系统架构图 |
| `tests/` | `test_models.py`（预置模型目录）、`test_network.py`（引擎不变量、结构检查）、`test_api.py`（HTTP + MCP 回调）、`test_users.py`（登录、重置密码、信任域、外溢审批、台账、看板）、`test_login.py`（公司账号登录：只认发起的浏览器、一次性、已有账号的绑定、谁能进来、关掉邮箱密码、`login.json` 出错不拖垮、OAuth2 驱动对假身份系统）、`test_joint.py`（共享项目、项目档案）、`test_flow.py`（信息流）、`test_archive_access.py`（资料的目录层级、资料与技能的可见范围，不变量 26）、`test_projects.py`（项目、助理、项目会话）、`test_assistant.py`（助理对话、起草、定时）、`test_migration.py`（旧数据迁移）、`test_rename.py`（改名兼容）、`test_attachments.py`（对话附件）、`test_skills.py`（技能）、`test_connections.py`（连接器）、`test_plugins.py`（项目插件）、`test_admin.py`（管理后台、用量台账）、`test_im.py`（IM 桥、绑定、不变量 25）、`test_wecom.py`（企业微信驱动对假长连接服务）、`test_dsh_check.py`（dsh 冒烟检查的配置解析与插件清单）、`test_questions.py`（运行中提问：问谁、委托方回答 / 转交 / 自动转给人、直接回复即回答）、`test_goals.py`（长期目标的续跑与停止、后台任务守护）、`test_send_file.py`（Agent 发文件给人：附在回复上、路径与上限、信息流、失败时不丢）、`test_device_materials.py`（端侧材料：清单 + 本机缓存、只传缺的、按轮限定、校验与清理）、`test_design.py`（设计变量只有一个来源、刻度、对比度、页头说明长度）、`test_markdown.py`（前端 Markdown 渲染的链接，要 node）、`test_releases.py`（发布与订阅）、`test_connector_writes.py`（连接器写操作逐次确认）、`test_links.py`（`add_link`）、`test_dashboard_system.py`（接入已有看板系统的示例）、`test_delivery_dashboard.py`（交付看板示例，走演示交付数据服务）、`test_web_fetch.py`（带白名单的网页抓取）、`test_plugin_services.py`（插件服务与挂点、项目 Wiki 示例）、`test_route.py`（route 挂点的四条规则）、`test_remote_agents.py`（外部平台 Agent：基础 / 完整接入、先等再绑、陌生会话与旧密钥拒绝、调用方授权透传、中断、传文件：暂存再 `send_file`、取文件、两个外部 Agent 沿委托传文件）、`test_public_agents.py`（公共 Agent：批准与冻结权限、撤回、服务面只读不写回、调用方自己的授权、委托为调用方、维护人只看用量）、`test_cloud_isolation.py`（云侧外层沙箱的命令拼装、开关与降级、执行单元的 dsh 状态目录与旧会话迁移）、`test_read_isolation.py`（真的起外层 bwrap：别人的私有目录、别的执行单元、档案、暂存文件、`vault.key` 都读不到）、`test_sandboxd.py`（沙箱管理器：请求校验、docker run 参数、运行时与失败关闭、标签、Unix socket 接口）、`test_migrate.py`（迁移与回退命令）、`test_store_durability.py`（state.json 写入止血：fsync、`.bak`、读不了退到上一份、kill -9 后可读、指标）、`test_sessions.py`（登录令牌只存哈希、过期、旧明文令牌升级不掉线、Secure cookie）、`test_db_store.py`（数据库存储：自动导入、增量保存、事件与轮次、导出回退、kill -9 后确认过的写入在；设 `MATRIX_TEST_PG` 另跑 PostgreSQL）、`test_blobs.py`（内容存储：本机与假 S3、SigV4 向量、读缓存校验、暂存区清理）、`test_blob_attachments.py`（附件进内容存储：只认本对话上传过的、先写字节再记引用、双读与搬迁回退、宽限期回收、配额）、`test_sandbox_plane.py`（云侧沙箱方式：令牌、单元绑定、材料与检查点、资料的可见范围、交付、容量与唤醒、重建恢复、对账、旧数据导入；`channels.py` 让不变量 8、15、22 的测试两种通道都跑）、`test_sandbox.py`（沙箱命令拼装）、`test_questions.py`（运行中提问：问谁、委托方回答 / 转交 / 自动转给人、直接回复即回答）、`test_goals.py`（长期目标的续跑与停止、后台任务守护）、`test_send_file.py`（Agent 发文件给人：附在回复上、路径与上限、信息流、失败时不丢）、`test_device_materials.py`（端侧材料：清单 + 本机缓存、只传缺的、按轮限定、校验与清理）、`test_design.py`（设计变量只有一个来源、刻度、对比度、页头说明长度）、`test_markdown.py`（前端 Markdown 渲染的链接，要 node）、`test_releases.py`（发布与订阅）、`test_connector_writes.py`（连接器写操作逐次确认）、`test_links.py`（`add_link`）、`test_dashboard_system.py`（接入已有看板系统的示例）、`test_delivery_dashboard.py`（交付看板示例，走演示交付数据服务）、`test_web_fetch.py`（带白名单的网页抓取）、`test_plugin_services.py`（插件服务与挂点、项目 Wiki 示例）、`test_route.py`（route 挂点的四条规则）、`test_teammates.py`（队友协作示例） |
| `REQUIREMENTS.md` | 全部需求、实现状态与核心思路 |
| `docs/reference/` | 当前实现的分主题说明（见上方“按需阅读”） |
| `docs/device-runtimes.md` | 云侧 / 端侧运行时、运行时能力报告、可达性与信任边界（草案） |
| `docs/projects.md` | 项目与项目会话的设计、旧数据迁移 |

## 常用命令

```bash
uv sync                                         # 安装依赖
uv run pytest -q                                # 全部测试（离线，不需要 dsh）
uv run matrix --runtime scripted                # 启动服务 http://127.0.0.1:8765（离线）
uv run matrix                                   # 使用 DSH 运行时（需 PATH 中有 dsh）
uv run matrix --models models.json              # 云侧只允许 models.json 里的预置模型（改文件即生效）
uv run matrix --connectors connectors.json      # 云端连接器目录（默认 <data>/connectors.json，改文件即生效）
uv run matrix --runtime scripted --plugins examples/plugins.json   # 部署方插件目录（默认 <data>/plugins.json）
uv run matrix --admins you@example.com                             # 管理后台 /admin 给谁看（默认最早注册的人）
uv run matrix --login login.json                                   # 用公司账号登录的配置（默认 <data>/login.json，改文件即生效），见 docs/login-driver-authoring.md
uv run matrix --runtime scripted --platforms examples/remote-agent/agent-platforms.json   # 外部 Agent 平台目录（演示平台：uv run python examples/remote-agent/demo_platform.py）
uv run matrix --fetch fetch.json                                   # 抓取白名单：云侧 Agent 能抓哪些内网地址（默认 <data>/fetch.json，改文件即生效）
uv run matrix-plugin check examples/plugins/sales-pipeline           # 本地校验一个插件目录或 zip（--strict：提醒也算失败）
uv run python examples/delivery-dashboard/setup_demo.py              # 搭交付看板演示数据（.matrix-dashboard-demo，见该目录 README）
uv run python examples/dashboard-system/setup_demo.py                # 搭“接入已有看板系统”演示数据（.matrix-board-demo，见该目录 README）
uv run python examples/llm-wiki/setup_demo.py                        # 搭项目 Wiki 演示数据（.matrix-wiki-demo，见该目录 README）
uv run python examples/teammates/setup_demo.py                       # 搭队友协作（Raft 式）演示数据（.matrix-team-demo，见该目录 README）
uv run matrix-dsh-check [--dsh-bin <dsh>] [--live]                   # 对真实 dsh 冒烟检查（升级 dsh 前后跑，--live 要调模型；含云侧读隔离检查）
MATRIX_CLOUD_ISOLATION=required uv run matrix                      # 云侧外层沙箱：required 没有就拒绝云侧运行 / preferred（默认）有就用 / off
uv run matrix --sandbox local                                      # 云侧沙箱方式（阶段 1a，默认关）：每个执行单元一个本机子进程沙箱，走端侧协议
uv run matrix-dsh-check --live --sandbox local                     # 同一组实际检查在沙箱方式下跑（另加 home、send-file、deliver、restore、isolation、resource-limit）
uv run matrix-sandboxd --config sandboxd.json --socket <路径>        # 沙箱管理器（docker 后端；部署见 deploy/compose.sandbox.yml）
uv run matrix migrate workdirs|export|purge-legacy [--yes]          # 沙箱方式的旧数据迁移与回退（服务停着跑，docs/cloud-runtime.md 第 10 节）
MATRIX_STORE=sqlite uv run matrix --runtime scripted                 # 状态存进 <data>/matrix.db（库空时自动导入 state.json）
uv run matrix migrate state|verify / export-state / backup-db --store sqlite   # 导入并对账、按内容比对、导回 state.json、在线备份（docs/reference/storage.md）
uv run python -m matrix.cli "我要研究一个新的 AI 产品机会。" --runtime scripted   # 在项目群聊里 @ Product Strategy Agent
uv run matrix-connect setup http://127.0.0.1:8765 --no-service   # 端侧连接器：设备码配对（浏览器里批准），检测本机全部 harness
uv run matrix-connect setup http://127.0.0.1:8765 --code <令牌> --no-service   # 凭一次性配对令牌直接配对（POST /api/devices/enroll 签发）
uv run matrix-connect setup http://127.0.0.1:8765 --agent 报销助手 --no-service   # 配对时一键创建一个绑在这台电脑上的 Agent
uv run matrix-connect run                                        # 前台运行已配对的连接器
uv run matrix-connect --url ws://127.0.0.1:8765/api/devices/connect --token <设备令牌> --driver dsh   # 手动令牌（备用）
```

## 工程约束

### 一、网络不变量（索引）

改代码时必须保持，均有测试覆盖。下面只是索引，全文（含细节与边界情况）见 [invariants.md](docs/reference/invariants.md)；
改动碰到哪一条，先读那一条的全文。

1. **权限只收窄不放大**：有效权限 = 自身权限 ∩ 委托授予 ∩ 运行时能强制的权限。
2. **只委托项目成员**：网络不自动创建 Agent，只接受提议、由人批准。
3. **约束沿委托链累加**。
4. **防环与限深**：委托链成环、超深度、超次数一律拒绝。
5. **只有直接委托方能验收**。
6. **协作只走网络**：Agent 之间的协作必须经过网络工具。
7. **上下文默认隔离**：每个席位、每个任务独立会话、独立工作目录。
8. **私有目录归 Agent 自己**：网络不为自己读取、不注入，只代 Agent 本人取用并记来源。
9. **知识只显式共享**。
10. **身份由网络认定**：凭执行上下文令牌，不信任模型自报。
11. **越权必留痕**。
12. **重启语义**：云侧运行中任务判失败可续接，端侧任务回到等待。
13. **归属与可见性**：无权限一律按“不存在”（404）。
14. **工作区外溢：云侧不可写，端侧由所有者批准**：云侧工作目录以外一律不可写、不审批；端侧逐次批准，只对那一次调用生效。
15. **并发不抢占**：私有目录写回按文件三方合并。
16. **个人 Agent 不跨信任域**：跨用户协作只能通过项目（邀请了别人就是共享项目）；例外只有公共 Agent（不变量 27）。
17. **项目档案只走网络**：写入只经人的操作或档案工具。
18. **项目会话只属于它的人**：不需要助理，@ 谁谁接；读项目状态按会话主人的可见性，被 @ 进来的别人的 Agent 的所有者只看得到经过它的那几轮。
19. **连接器凭证只属于授权的人**：网络代调用，共享项目里用到时先问授权人；按 MCP 标注会对外产生影响的写操作每次先给授权人确认。
20. **插件页面代码只在浏览器沙箱里**：只经网络、以看的人的身份读写，以人名义的动作逐次确认。
21. **助理对话只替主人读与起草**：读到共享项目内容的那一轮不能调用连接器；定时只属于主人，定时轮次不能登记用户任务。
22. **信息只流向当时在场的人**：私有目录按来源场合挂载，用到别处资料的输出先给所有者确认，判断不用模型。
23. **发布只经主持人，交付件只流向读者**：项目内容离开项目只经发布物；订阅只在订阅者自己的 Agent 上、能到达的人都看得到时生效。
24. **插件服务只来自部署方目录，只经网络、按挂点作用**：一个安装一个进程，不拿凭证；工具、只读挂载、标明来源的上下文、验收后的通知，只在本项目里生效。
25. **IM 只是绑定的人的入口**：只认核验过绑定码的那个身份在单聊里说的话，推出去的只到他自己的单聊、只是他本来看得到的。
26. **资料与项目技能只给可见范围里的 Agent**：默认所有成员可见，主持人可收窄到角色 / 成员；目录逐级只收窄，挂载、材料、推送、群聊事件都按它过滤。
27. **公共 Agent 只在服务面跨信任域**：管理员批准、冻结权限；服务面只为调用方做事，私有目录只读不写回、不列别处、不外溢、用调用方自己的授权、过程只给调用方，维护人只看用量。
28. **外部平台 Agent 的回调只落到 Matrix 发起的那一轮**：平台只来自部署方目录；回调凭 Matrix 签的密钥 + Matrix 发起且还没结束的会话 id；调用方授权只透传他自己的，缺了不发起。

### 二、工作方式

1. 先读后改：改引擎前读 `engine.py` 与相关测试；保持现有风格（dataclass、类型标注、中文 docstring 与提示词、小工具函数）。
2. 新行为或修 bug 先补测试，用 `ScriptedRuntime` 写确定性测试，不依赖真实 DSH 或网络。
3. 每次改动后运行 `uv run pytest -q`，全部通过再交付。PR 与 main 上的提交由 GitHub Actions 在 Linux、Windows、macOS 上各跑一遍
   （`.github/workflows/test.yml`），某一端的问题看那里的结果，不必另找机器手动验证。读写文本、子进程的文本管道一律写
   `encoding`：CI 设了 `PYTHONWARNDEFAULTENCODING=1`，漏写的在三端都会变红（本地加上这个环境变量跑也能查）。
4. 新增权限时同步改三处：`models.AUTHORITIES`、`models.TOOL_AUTHORITY`、`runtime.AUTHORITY_PLUGINS`；
   `test_authorities_registered_everywhere` 会在漏改时变红。
5. 新增网络工具时同步改：`mcp_server.TOOLS`（schema）、`engine.TOOL_HANDLERS`（实现）、`protocol.TOOL_GUIDE`（提示词）；
   `test_network_tools_defined_consistently` 会在漏改时变红。工具里的自由文本参数一律用 `string`，不要放进数组
   （DeepSeek 下数组参数里的长中文容易写成裸文本，导致 `MALFORMED_RESPONSE`）。
6. 状态结构有变化时保证 `Store._load` 能读旧的 `state.json`（`store._known` 会丢弃未知字段、缺失字段取默认值）。
   数据库存储读回来也走 `_load`，同样适用；新增一类状态（`Store` 上新的字典）时同步加进 `store.ENTITY_KINDS`（`test_db_kinds_cover_snapshot`）。
   改消息一律整体赋值，不要原地改 `Message` 的列表字段（数据库存储靠赋值记改动，docs/reference/storage.md）。
7. 每条网络不变量都要有测试，测试 docstring 写“不变量 N”；`test_invariants_have_tests` 在新增不变量却没写测试、
   或 [invariants.md](docs/reference/invariants.md) 与下方索引不一致时变红。
8. 规则反复靠记忆失效时，把它挪进代码或测试（参照 Raft 的 structural enforcement，research: raft#structural-enforcement）：一条规则如果违反了也没有任何信号，
   就要么加一个能变红的检查，要么写明为什么接受这个风险。
9. 不要提交 `.matrix/`（或改名前的 `.agentnet/`）下的运行数据；那里可能包含真实研究结果与会话记录。
10. 依赖使用精确或下限版本，经 `uv add` 写入 `pyproject.toml` 与 `uv.lock`。
11. 设计借鉴了别的产品时写“参照 X”并带 `research: <笔记>#<锚点>`（见文首），调研笔记里没有的先补笔记。
    `test_research_refs_resolve` 在调研仓库就位时检查每个引用都能找到对应的笔记与锚点，不在时跳过。
12. 系统架构或设计有变化（新增或拆分模块、运行时、接口、状态目录、不变量，改变协作方式）时，同一个改动里刷新
    [about.html](matrix/static/about.html) 的架构图与相关段落。架构图是唯一的对外架构说明，不另放静态架构图。
    `test_about_page_matches_invariants` 只能核对不变量条数，其余靠改动者对照这一条自查（内容是给人看的介绍，逐项机器核对得不偿失）。

### 三、安全红线

全文与已知风险见 [security.md](docs/reference/security.md)。

- 服务只监听 `127.0.0.1`，不要暴露到公网或改成 `0.0.0.0`。
- 不要在日志、事件或 API 返回里带出密码、哈希、登录令牌；状态里的登录令牌只存 sha256（`auth.session_hash`），不要改回明文作键。
- 公司账号登录：一次登录只认发起它的浏览器（一次性 cookie + state），不要放宽；外部系统带着用户凭据调接口（`api_auth`）只认 `Authorization` 请求头、只经驱动的 `verify` 向身份系统核实，不要改成直接读浏览器带来的共享 cookie、不要信凭据里自称的用户，`/api/admin/*` 不认它；邮箱撞上已有账号默认不绑（`link_by_email` 由部署方打开），不要改成默认绑；身份系统的地址与密钥只来自部署方的 `login.json`（密钥经名字带 `KEY` 的环境变量），不要做成用户可填。
- `/internal/tools`、`/internal/approval`、`/internal/question`、`/internal/connectors/*` 只凭有效执行上下文令牌调用，不要放宽；`/mcp/remote` 调工具（`tools/call`）只凭“Matrix 签的密钥 + Matrix 发起且还没结束的平台会话 id”，不要放宽成只凭密钥（只有不执行东西的 `initialize` / `ping` / `tools/list` 为了平台试连不要会话 id）；外部平台地址只来自部署方的平台目录；设备配对接口的上限（`auth.PAIR_*`、`auth.ENROLL_*`）不要去掉；一次性配对令牌只能兑换一次、只存 sha256，不要改成可重复用或长期有效，也不要让业务系统转交长期设备令牌代替它。
- 不要绕过 `build_patch_yaml` 生成的沙箱限制，也不要让审批应答者自动批准（端侧）、提问应答者替人作答；云侧覆盖层不插审批应答者、`/internal/approval` 对云侧一律 `unavailable`（不变量 14）；覆盖层要一直关掉 dsh 的默认技能根。
- 云侧 dsh 的外层沙箱里只挂这个执行单元的工作目录与 dsh 状态目录（可写）和运行要的代码（只读）：不要把数据目录、别的执行单元或共用的 `~/.dsh` 挂进去，也不要因为某个功能要读别处就放宽（`outer_argv`，`matrix-dsh-check` 的 `read-isolation` 变红就不能上线）。
- 连接器凭证只进 `vault/`，不要写进运行时的环境、覆盖层、工作目录；模型供应商密钥也只进 `vault/`，唯一的出口是那一轮 dsh 进程的环境变量（变量名要带 `KEY`，`matrix-dsh-check` 的 `key-hidden` 变红就不能升级 dsh），不要写进覆盖层或下发到端侧；用户自定义的连接器地址只能经 `mcp_client.check_public_url` / `public_only` 连 https 公网（网络会被当跳板访问内网），唯一的例外是部署方在 `connectors.json` 的 `allow` 里写的内网目标（可以用 http），不要另开口子；
  能抓哪些内网地址只来自部署方的 `fetch.json`（`web_fetch.plan` 每一跳都校验），不要让用户或 Agent 改它。
- IM 机器人的密钥只进 `vault/`；桥只放行绑定的人在单聊里说的话、只往他自己的单聊推，不要放宽；企业微信地址只由部署方经 `MATRIX_WECOM_URL` 改，不要做成用户可填（网络会被当跳板）。
- 沙箱方式（`MATRIX_CLOUD_SANDBOX`）下沙箱交回来的东西都不可信：产物清单、私有目录改动、会话包只经 `checkpoints.clean_manifest`、`Network._apply_home_changes`、`Uploads`（路径、大小、sha 校验）进入网络的状态，不要绕过；沙箱连接只接受派给它那个执行单元的运行（`DeviceRuntime.unit`），不要放宽。暂定的不变量 27（执行面与控制面分离）只在开关打开时成立，所以没进不变量表，等默认切换时再加。
- 已知风险：云侧读隔离只做到阶段 0（单机止血，外层 bwrap）：文件读隔离有了，内核与网络仍共用、没有 cgroup 配额、模型密钥仍在 dsh 环境变量里，没有外层沙箱的部署（`preferred` 降级、`off`）仍是“沙箱只管写不管读”，插件服务里目录托管的进程也读得到数据目录。多机部署、接真实账号之前要做完 [cloud-runtime.md](docs/cloud-runtime.md) 的阶段 1–2，至少用 `MATRIX_CLOUD_ISOLATION=required`。
