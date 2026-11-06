# 云侧与端侧运行时：当前实现要点

> 入口见 [AGENTS.md](../../AGENTS.md)。设计方向见 [docs/device-runtimes.md](../device-runtimes.md)。

- Agent 可以跑在**平台云侧**（内置 dsh）或**用户端侧**（用户自己的 Claude Code / Codex 等 harness）。
  网络对两者**同等看待**：位置只是节点属性，不改 Seat / Task / Approval / 知识 / 项目成员的语义。
- 端侧的接入方式是用户电脑上的**出站连接器**（不开入站端口），按用户配对：一个用户可以有**多条**
  设备令牌（只存 sha256，可单独吊销），连接器用令牌连 `GET /api/devices/connect`
  （WebSocket，令牌走 query）。网络工具经连接器回传，身份仍由执行上下文令牌认定；
  审批上行进入现有 Approval 记录，由 Agent 的所有者决定。
  提问（dsh 的 `ask_user_question`）同样上行：本机桥 `/internal/question` → `question.request {run_id, call_id, questions}` →
  `Network.request_question`，回执 `{answers}` 或 `{error}`。`run.request.questions` 告诉驱动这一轮能不能提问，握手里的
  `capabilities.questions` 声明驱动能不能把提问转回来（旧连接器不报告 = 不能；见 [execution.md](execution.md)“运行中提问”）。
- **一台电脑一个连接器，一机多 harness**（参照 Raft Computer，research: local-harness-integration#raft）：
  握手报告本机全部 harness（`hello.ok.runtimes`），Agent 绑定“设备 + harness”（`Agent.harness`，取
  `models.HARNESSES` 的键，`None` = 设备默认）。有效权限按绑定的 harness 计算；设备上没有那个 harness 时按不可达等待，
  不换到别的 harness 上跑。
- **接入走设备码配对**（参照 xmatrix，research: xmatrix#runtime-access）：`matrix-connect setup <服务器>` 申请配对码，
  人在已登录的网页（`#pair=<码>`）里批准，令牌经轮询只交付一次，存进本机 0600 配置文件；再装成 launchd / systemd 用户服务（Windows 是任务计划程序里的登录任务）。
  `/api/devices/pair` 与 `…/pair/poll` 不需要登录，所以同时等待的申请数、每个用户输错配对码的次数都有上限（`auth.PAIR_*`），
  不要去掉。手动签发令牌（`POST /api/devices`）留作没有浏览器时的备用。
- **升级不重新配对**：后台服务定义指向装它时的那份安装（`service.runs_from` 看得出来，`matrix-connect status` 会提示）。
  换了安装位置（代码包从 agentnet 改名为 matrix 后，`uv tool install --force git+…` 装进了新的工具目录）要运行
  `matrix-connect install`：按本机已有配置重新装服务，设备和绑在它上面的 Agent 不变；不要重跑 `setup`（那会配对成一台新设备）。
  服务名（`dev.matrix.connect` / `matrix-connect.service` / `MatrixConnect`）没变，重新装会原地替换。
  还没升级的连接器照样能连：它报回的旧工具名（`mcp__agentnet__*`）由网络换成现在的（`compat.current_tool`）。
- **有效权限要再交运行时强制执行能力**：只有该 harness 能强制的权限才进入有效权限，
  只能靠提示词约定的按 `advisory` 记账、自动收窄，并在委托详情里写明原因。
- 私有目录在端侧就留在用户电脑上（`<root>/.homes/<agent>/`），网络不读、不注入；服务器只在 `run.request.home`
  里说明挂载方式（`rw` / `ro` / 不挂载），连接器按与云侧相同的语义（`matrix/homedir.py`）挂载与写回。项目文件按轮下发只读副本。
- **端侧不可达是等待，不是失败**：任务进入 `WAITING`，不消耗重试，无限等；对话状态仍是 `waiting`；
  端侧席位只给所有者写一条“已排队”说明。上线后（`register_runtime`）唤醒该设备上等待中的任务，消费积压。
  离线期间按最近一次握手声明的能力计算有效权限（`Agent.enforcement`），不把“未知”当成“全都不行”。
- **A2A 只用于网络边界**（外部 A2A 网络、多套 Matrix 之间），内部 Agent 协作仍然只走网络工具；
  外部请求进来的任务不获得本系统内的授权。
- 当前状态：**运行时契约、能力报告、连接器协议与配对已实现**（`runtime.RuntimeCapabilities`、
  `engine.Network.capabilities`/`_effective`/`runtime_for`、`Task.narrowed`、`device.py`、设备令牌与
  `/api/devices/connect`）。**连接器本体已实现**：`uv run matrix-connect`（`matrix/connector/`），dsh、
  Claude Code、Codex 三个驱动，已用本机 dsh 与 Claude Code 跑通端到端，dsh 与 Codex 也在 Windows 与 WSL 上跑通
  （Windows 上 npm 装的 harness 是 `.cmd` 包装，`runtime.argv` 直接用 node 跑；Windows 的后台服务是任务计划程序里的登录任务，用 pythonw 跑、日志写文件）。**端侧其余部分也已实现**：
  设备管理页（配对 / 吊销、在线状态、harness 列表）、Agent 绑定“设备 + harness”与能力缺口展示、材料下发（清单 + 本机缓存，见下文“材料”）
  与 `publish_file` 回传（`file.fetch`）、设备码配对与后台服务（需求 #41）。**还没有**：云侧的读隔离（需求 #34）。

## 材料：清单 + 本机缓存（只传缺的）

端侧每轮要的只读材料（项目档案文件、这个席位看得到的对话附件、订阅的交付件、技能、插件挂载）原来每轮整份 base64 塞进 `run.request.materials`，
连接器先删后写：一个带过几个大附件（单个 ≤20 MB）的对话，Agent 每被 @ 一次就重传几十 MB。现在：

- 服务端（`engine._materials` → `runtime.Material`）只列清单：路径、sha256、大小。对话附件按内容寻址，文件名就是 sha；其余文件的 sha
  按（路径、修改时间、大小）缓存在进程里，没改的不重算。内容留在服务器上，不进内存。
- 连接器在握手里声明 `hello.ok.features: ["materials-cache"]`，`run.request` 就只带 `manifest: [{path, sha, size}]`；
  旧连接器不声明，照旧整份内联（`materials: [{path, data}]`）。
- 连接器在**运行线程**里（不能占着接收循环，要等回执）按清单补齐：`<工作根目录>/.cache/materials/<sha>` 里有的直接拷进工作目录；
  没有的用 `material.fetch {run_id, sha, offset}` 分块取（每块 1 MB），取完校验大小与 sha256 再放进缓存，对不上就这一轮按传输错误失败、
  不留坏文件。拷贝而不是硬链接：Agent 改了工作目录里的副本，缓存不跟着坏。缓存超过 2 GB 按最近使用清最旧的（这一轮用到的不清）。
- 服务端只回**这一轮清单里**的 sha，轮次结束就取不到（`DeviceRuntime._blobs`）；路径、sha、大小在连接器那边再校验一次，
  私有目录（`agent-home/`）与越界路径一律拒绝。工作目录引用不能以 `.` 开头，缓存不会落进任何工作目录。
- 结果：没变的档案文件、附件第二轮起一个字节都不传，新附件每台设备只传一次（`tests/test_device_materials.py`）。
  每轮开始先清掉上一轮的只读副本（`project/`、`attachments/`、`skills/`、`releases/`、`plugins/`）。

<a id="cloud-vs-device"></a>
## 云侧与端侧的差异

网络对两者同等看待（席位、委托、审批、提问、知识、项目成员的语义一样），差异只在“执行面在哪、怎么拿到东西、靠什么隔离”：

| | 云侧（服务器上的 dsh） | 端侧（用户电脑上的 harness） |
| --- | --- | --- |
| 进程 | Matrix 服务的子进程：同一台机器、同一个操作系统用户；所有用户的云侧 Agent 都在这里 | 用户电脑上连接器（`matrix-connect`）起的 dsh / Claude Code / Codex，以用户本人身份运行 |
| 工作目录 | 在数据目录里：`<data>/conversations/<对话>/…`、`<data>/tasks/<任务>/` | `<工作根目录>/<agent>/<场合>`，在用户电脑上 |
| 只读材料（档案文件、附件、订阅、技能、插件挂载） | 每轮从数据目录整份拷进工作目录，运行后丢弃（`_run` 里的 `_mount_*`） | 清单 + 本机缓存，只传缺的（上一节） |
| 私有目录 | `<data>/agents/<id>/` 每轮整份拷进 `agent-home/`，运行后按文件三方合并写回；越界来源的笔记不挂（不变量 22） | 留在用户电脑 `<根目录>/.homes/<agent>/`，连接器按同样的语义挂载与写回，网络碰不到；还没有来源标签，有别人的项目里不挂 |
| 沙箱 | dsh 的 bwrap：**只管写不管读**，写只能写工作目录 | 看 harness：dsh 同云侧；Codex `workspace-write`；Claude Code 没有（权限按 `advisory` 收窄）。读的是用户自己的电脑 |
| 网络工具 / 审批 / 提问 | stdio MCP 回调本机 `/internal/*`，凭一次性令牌 | 本机小桥（`connector/bridge.py`）→ WebSocket → 同一套逻辑，凭 `run_id` |
| 产物 | 交付时拷进委托方工作目录 `deliveries/`；人可以浏览工作目录 | 留在设备上；要给别人用 `publish_file` / `send_file`，服务器经 `file.fetch` 回读 |
| 模型凭据 | 平台配的供应商密钥只经那个 dsh 进程的环境变量（名字带 `KEY`，dsh 不传给 Agent 的命令） | harness 自己的登录，Matrix 不下发任何密钥 |
| 不在线 | 不存在 | 任务进入 `WAITING`、不消耗重试，上线后消费积压 |

<a id="cloud-debt"></a>
### 云侧现状：粗放，隔离性差（待优化）

云侧是先跑通的那一套，执行面和控制面挤在一起。端侧反而已经是“执行面只拿网络给的副本、只经令牌回调”的形状。云侧要对齐的地方：

1. **读隔离缺失（最要紧）**：沙箱只管写不管读，云侧 Agent 能读到整个 `--data`（别人的私有目录、对话、任务、`vault.key`），
   插件服务同理。见 [security.md](security.md) 第一条与需求 #59（[plugin-hooks.md](../plugin-hooks.md#isolation-debt)）：
   控制面与执行面分开、每个执行单元（至少 Agent × 场合）一个沙箱、插件服务同样进沙箱。团队协作、多人部署之前必须完成。
2. **只读材料每轮整份拷贝**：档案文件、对话里全部可见附件（单个 ≤20 MB）、订阅、技能、插件挂载每轮拷进工作目录再删掉，
   一个对话里每个席位每轮都拷一遍。本机磁盘，不走网络，但量随对话变长线性增长。方向：和端侧一样按 sha 复用——
   做了每执行单元的沙箱后，内容寻址的只读存储直接只读挂进沙箱，不再拷贝。
3. **私有目录每轮整份拷入、整份比对写回**：开销随私有目录大小增长。方向同上：沙箱里直接挂载（只读 / 读写）。
4. **工作目录在数据目录里**：执行面的文件和网络的状态放在一起，挪不走也隔不开。方向：执行面单独的目录树（或单独的机器 / 容器）。
5. **没有资源上限**：只有运行时长（`timeout_s`），没有 CPU、内存、磁盘上限，一个 Agent 能拖垮整台服务器。方向：cgroup 或容器配额。
6. **模型密钥经 dsh 进程的环境变量**：靠 dsh 按名字过滤挡住 Agent 的命令；没有独立的 PID 命名空间时，Agent 可能经 `/proc` 读到父进程环境
   （`matrix-dsh-check` 的 `key-hidden` 一项检查，部署容器里必须是绿的）。方向：模型调用经网络代理，密钥不进执行面。

需求表里的对应条目：#59（读隔离）、#68（材料与私有目录不再每轮整份拷贝、资源上限）。

