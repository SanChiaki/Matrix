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
- 状态：全部持久化在 `--data` 目录（默认 `.matrix/`，只有改名前的 `.agentnet/` 时沿用它；都已被 gitignore）的 `state.json`；对话、任务各有独立工作目录 `conversations/<id>/`、`tasks/<id>/`；每个 Agent 有持久私有目录 `agents/<id>/`；每个项目有档案目录 `projects/<id>/`（`files/` 与变更日志 `log.jsonl`），不放进任何代码仓库。

核心概念（定义见 [domain.md](docs/reference/domain.md)）：

- **Agent**：网络里的节点，只属于、只为它的所有者工作；有自身权限上限，可跑在云侧或用户端侧。每个人有一个**助理**（自己的一个 Agent，注册即有），进项目时自动加入。
- **助理对话**：侧栏最上面的“助理”，和自己助理的私聊。助理在这里看得到你各个项目的进展，替你起草要发进项目的消息（你确认才发），按你的话定时提醒（默认工作日早报）。
- **项目**：成员集合与委托边界，没有主 Agent，也没有类型：邀请了别人就是共享的，这是跨用户协作的唯一途径。
- **对话**：每个项目一个群聊（被 @ 的成员接任务）；人在项目里可以开多个**项目会话**，和自己的助理私下聊、带项目上下文；每个 Agent 一个私聊。**席位**是 Agent 在一个对话里的执行上下文。
- **委托（Task）**：Agent ↔ Agent 的一次授权，异步执行、收件箱唤醒，由委托方验收。
- **用户任务（UserRequest）**：人交代的有交付目标的事，看板上只有它。
- **项目档案**：说明（纲要、已定的事）、资料（文件、链接）、项目技能，归项目所有，按版本推送给成员。
  项目页另有**连接器面板**：这个项目用到了哪些外部服务、每个成员能不能用，项目从不持有谁的授权。
- **插件**：项目装上的业务扩展（表、沙箱页面、打法、触发），只往群聊里 @ 成员，不硬编排流程。

## 按需阅读

| 要改 / 要了解 | 先读 |
| --- | --- |
| 模型字段、状态结构、权限词表 | [docs/reference/domain.md](docs/reference/domain.md) |
| 群聊、项目会话、私聊、@ 唤醒、成员变动、共享项目与可见性 | [docs/reference/conversations.md](docs/reference/conversations.md) |
| 委托、收件箱、失败与续接、看板与用户任务 | [docs/reference/execution.md](docs/reference/execution.md) |
| 项目档案与档案工具 | [docs/reference/archive.md](docs/reference/archive.md) |
| 项目、助理、项目会话、旧数据迁移 | [docs/reference/projects.md](docs/reference/projects.md)，设计见 [docs/projects.md](docs/projects.md) |
| 助理对话：跨项目总览、起草、定时与早报 | [docs/reference/assistant.md](docs/reference/assistant.md) |
| 项目插件（表、沙箱页面与桥、打法、触发） | [docs/reference/plugins.md](docs/reference/plugins.md)，设计见 [docs/project-plugins.md](docs/project-plugins.md)，给插件作者的指南见 [docs/plugin-authoring.md](docs/plugin-authoring.md)（改了清单字段、SDK、触发行为要同步改它） |
| 连接器（外部服务）与技能，个人与项目两层，项目的连接器面板与共享项目里的授权 | [docs/reference/connectors-skills.md](docs/reference/connectors-skills.md)，设计见 [docs/connectors-and-skills.md](docs/connectors-and-skills.md) |
| 端侧运行时、Matrix 客户端（`matrix-connect`）、设备配对、harness | [docs/reference/device-runtimes.md](docs/reference/device-runtimes.md)，设计见 [docs/device-runtimes.md](docs/device-runtimes.md) |
| 任何会改变引擎行为的改动 | [docs/reference/invariants.md](docs/reference/invariants.md) 里相关条目的全文 |
| 登录、`/internal/*`、沙箱、文件下载、部署 | [docs/reference/security.md](docs/reference/security.md) |
| 需求与实现状态 | [REQUIREMENTS.md](REQUIREMENTS.md) |

## 模块地图

| 文件 | 职责 |
| --- | --- |
| `matrix/engine.py` | `Network` 核心：Agent/项目/对话管理、项目的参与者与可见性、项目档案、助理与项目会话、助理对话（跨项目总览、起草、个人定时）、@ 解析、收件箱调度、席位与任务轮次、委托授权、验收与续接、提议审核、私有目录挂载、信息流（来源标签、`open_note`、拦截确认）、知识可见性、违规检测、项目插件（安装、记录、桥、触发） |
| `matrix/protocol.py` | 对话 / 任务 / 通知提示词、交付与失败说明、`TOOL_GUIDE`（网络工具用法） |
| `matrix/runtime.py` | `AgentRuntime` 抽象；`DshRuntime`（`--patch` 覆盖层按权限禁用插件 + 沙箱 + 挂载 MCP + 按 Agent 设模型）；`ScriptedRuntime`；运行时错误分类 |
| `matrix/mcp_server.py` | 仅标准库的 stdio MCP 服务，`TOOLS` 是网络工具 schema 的唯一定义，回调 `/internal/tools` |
| `matrix/server.py` | FastAPI：登录、REST（按用户过滤）、看板 `/api/board`、SSE 事件流 `/api/events`、静态页面、运行时回调 `/internal/tools` 与 `/internal/approval` |
| `matrix/auth.py` | 用户注册 / 登录 / 退出（scrypt，仅标准库）；第一个注册的用户接管已有数据 |
| `matrix/dsh_plugins/approval.mjs` | dsh 审批应答者：沙箱升权申请转给网络，由 Agent 所有者决定 |
| `matrix/connector/` | 端侧连接器 `matrix-connect`：出站 WebSocket、本机桥（`/internal/*` 转发出站）、dsh / Claude Code / Codex 驱动（一个连接器同时提供多个）、设备码配对（`pairing.py`）、本机配置（`config.py`）、后台服务（`service.py`） |
| `matrix/skills.py` | 技能文件（`SKILL.md` + 附带文件）的读写校验、按层合并（项目 > 连接器自带 > 我的）与挂载 |
| `matrix/plugins.py` | 项目插件：清单（`plugin.json`）与包的校验、安装、`page/` 取文件、记录的存取与按清单校验、cron；部署方插件目录 `plugins.json`（`--plugins` / `MATRIX_PLUGINS`，热加载）。引擎里的安装、桥、触发在 `engine.py`「项目插件」一节 |
| `matrix/plugin_check.py` | `matrix-plugin check`：本地校验插件包（错误用安装时同一套校验，另给启发式提醒：桥客户端路径、外部资源与联网、没申请的能力、触发消息里会变空的字段） |
| `matrix/dsh_check.py` | `matrix-dsh-check`：对真实 dsh 的冒烟检查，升级云端 dsh 前后用（静态：覆盖层引用的插件都在、新工具插件提醒、覆盖层能加载；`--live`：回复、续接、技能、委托、连接器、沙箱审批） |
| `matrix/static/plugin-sdk.js` | 插件页面里的桥客户端（`window.matrix`），经 `postMessage` 让父页面转给网络 |
| `matrix/connector_catalog.py` | 云端连接器目录：部署方维护的 `connectors.json`（`--connectors` / `MATRIX_CONNECTORS`），热加载；地址只来自这里 |
| `matrix/vault.py` | 连接器凭证的加密保管（Fernet，`MATRIX_VAULT_KEY` 或 `<data>/vault.key`） |
| `matrix/mcp_client.py` | 网络这一侧的远程 MCP 客户端（Streamable HTTP，JSON / SSE），代调用云端连接器 |
| `matrix/mcp_connectors.py` | 运行时这一侧的 stdio 桥：把连接器工具转发到 `/internal/connectors/*`，只带执行上下文令牌 |
| `matrix/model_catalog.py` | 云侧预置模型目录：部署方维护的 `models.json`（默认 `<data>/models.json`，`--models` / `MATRIX_MODELS`），按修改时间热加载；启用后云侧 Agent 只能选目录里的模型，下架的改用默认（`Network.model_info` / `run_model`）；端侧不受约束；没有文件时照旧自由填写 |
| `matrix/workdir.py` | 工作目录里的“产物”口径：委托产物快照（`deliveries/`）、目录打包发布、人浏览工作目录 |
| `matrix/homedir.py` | 私有目录的挂载与三方合并写回，云侧（engine）与端侧（connector）共用 |
| `matrix/flow.py` | 信息流的判断（不变量 22）：私有目录文件的来源标签、在场的人、挂载与放行，纯函数集合比较，不用模型 |
| `matrix/store.py` | 持久化、旧数据迁移（`_merge_groups`：群组 + 项目两层合成一层，`_one_chat_each`、`_one_trust_domain`）与种子数据（5 个种子 Agent + 一个研究项目；服务默认不预置，CLI 与测试预置） |
| `matrix/compat.py` | 改名兼容（代码原名 agentnet）：部署方环境变量 `MATRIX_*` 没设时读 `AGENTNET_*`（`env`）、默认数据目录沿用旧的 `.agentnet`（`data_dir`）、旧网络工具名换成 `mcp__matrix_net__*`（`current_tool`，用于旧 state.json 与没升级的连接器）。改名相关的兼容都放这里 |
| `matrix/events.py` | 进程内事件总线（带 seq，供 SSE 与 CLI 消费） |
| `matrix/scripted.py` | 脚本运行时的场景处理器 |
| `matrix/cli.py` | 无 UI 跑一轮对话并打印事件流 |
| `examples/plugins/` | 示例插件（销售漏斗：表、看板页面、打法、三种触发）；`examples/plugins.json` 是对应的部署方目录 |
| `matrix/static/` | 单页前端（原生 JS）；`about.html` 是产品介绍页（`/about`），含可展开的系统架构图 |
| `tests/` | `test_models.py`（预置模型目录）、`test_network.py`（引擎不变量、结构检查）、`test_api.py`（HTTP + MCP 回调）、`test_users.py`（登录、信任域、外溢审批、台账、看板）、`test_joint.py`（共享项目、项目档案）、`test_flow.py`（信息流）、`test_projects.py`（项目、助理、项目会话）、`test_assistant.py`（助理对话、起草、定时）、`test_migration.py`（旧数据迁移）、`test_rename.py`（改名兼容）、`test_attachments.py`（对话附件）、`test_skills.py`（技能）、`test_connections.py`（连接器）、`test_plugins.py`（项目插件）、`test_dsh_check.py`（dsh 冒烟检查的配置解析与插件清单） |
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
uv run matrix-plugin check examples/plugins/sales-pipeline           # 本地校验一个插件目录或 zip（--strict：提醒也算失败）
uv run matrix-dsh-check [--dsh-bin <dsh>] [--live]                   # 对真实 dsh 冒烟检查（升级 dsh 前后跑，--live 要调模型）
uv run python -m matrix.cli "我要研究一个新的 AI 产品机会。" --runtime scripted   # 在项目群聊里 @ Product Strategy Agent
uv run matrix-connect setup http://127.0.0.1:8765 --no-service   # 端侧连接器：设备码配对（浏览器里批准），检测本机全部 harness
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
14. **工作区外溢由所有者批准**：逐次批准，只对那一次调用生效。
15. **并发不抢占**：私有目录写回按文件三方合并。
16. **Agent 不跨信任域**：跨用户协作只能通过项目（邀请了别人就是共享项目）。
17. **项目档案只走网络**：写入只经人的操作或档案工具。
18. **项目会话只属于它的人**：助理按所有者的可见性读取项目状态。
19. **连接器凭证只属于授权的人**：网络代调用，共享项目里用到时先问授权人。
20. **插件代码只在浏览器沙箱里**：只经网络、以看的人的身份读写，以人名义的动作逐次确认。
21. **助理对话只替主人读与起草**：读到共享项目内容的那一轮不能调用连接器；定时只属于主人，定时轮次不能登记用户任务。
22. **信息只流向当时在场的人**：私有目录按来源场合挂载，用到别处资料的输出先给所有者确认，判断不用模型。

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
- 不要在日志、事件或 API 返回里带出密码、哈希、登录令牌。
- `/internal/tools`、`/internal/approval`、`/internal/connectors/*` 只凭有效执行上下文令牌调用，不要放宽；设备配对接口的上限（`auth.PAIR_*`）不要去掉。
- 不要绕过 `build_patch_yaml` 生成的沙箱限制，也不要让审批应答者自动批准；覆盖层要一直关掉 dsh 的默认技能根。
- 连接器凭证只进 `vault/`，不要写进运行时的环境、覆盖层、工作目录，用户自定义的连接器地址只能经 `mcp_client.check_public_url` / `public_only` 连 https 公网（网络会被当跳板访问内网），不要放宽。
- 已知风险：沙箱只管写、不管读。接入第二个运行时或任何多机部署之前，必须先做读隔离。
