# 云侧与端侧运行时：当前实现要点

> 入口见 [AGENTS.md](../../AGENTS.md)。设计方向见 [docs/device-runtimes.md](../device-runtimes.md)。

- Agent 可以跑在**平台云侧**（内置 dsh）或**用户端侧**（用户自己的 Claude Code / Codex 等 harness）。
  网络对两者**同等看待**：位置只是节点属性，不改 Seat / Task / Approval / 知识 / 群组成员的语义。
- 端侧的接入方式是用户电脑上的**出站连接器**（不开入站端口），按用户配对：一个用户可以有**多条**
  设备令牌（只存 sha256，可单独吊销），连接器用令牌连 `GET /api/devices/connect`
  （WebSocket，令牌走 query）。网络工具经连接器回传，身份仍由执行上下文令牌认定；
  审批上行进入现有 Approval 记录，由 Agent 的所有者决定。
- **一台电脑一个连接器，一机多 harness**（参照 Raft Computer，research: local-harness-integration#raft）：
  握手报告本机全部 harness（`hello.ok.runtimes`），Agent 绑定“设备 + harness”（`Agent.harness`，取
  `models.HARNESSES` 的键，`None` = 设备默认）。有效权限按绑定的 harness 计算；设备上没有那个 harness 时按不可达等待，
  不换到别的 harness 上跑。
- **接入走设备码配对**（参照 xmatrix，research: xmatrix#runtime-access）：`matrix-connect setup <服务器>` 申请配对码，
  人在已登录的网页（`#pair=<码>`）里批准，令牌经轮询只交付一次，存进本机 0600 配置文件；再装成 launchd / systemd 用户服务（Windows 是任务计划程序里的登录任务）。
  `/api/devices/pair` 与 `…/pair/poll` 不需要登录，所以同时等待的申请数、每个用户输错配对码的次数都有上限（`auth.PAIR_*`），
  不要去掉。手动签发令牌（`POST /api/devices`）留作没有浏览器时的备用。
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
  Claude Code、Codex 三个驱动，已用本机 dsh 与 Claude Code 跑通端到端，dsh 与 Codex 也在 Windows 与 WSL 上跑通
  （Windows 上 npm 装的 harness 是 `.cmd` 包装，`runtime.argv` 直接用 node 跑；Windows 的后台服务是任务计划程序里的登录任务，用 pythonw 跑、日志写文件）。**端侧其余部分也已实现**：
  设备管理页（配对 / 吊销、在线状态、harness 列表）、Agent 绑定“设备 + harness”与能力缺口展示、材料下发（`run.request.materials`）
  与 `publish_file` 回传（`file.fetch`）、设备码配对与后台服务（需求 #41）。**还没有**：云侧的读隔离（需求 #34）。
