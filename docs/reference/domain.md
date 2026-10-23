# 领域模型

> 入口见 [AGENTS.md](../../AGENTS.md)。代码在 `matrix/models.py`。

| 概念 | 含义 |
| --- | --- |
| User | 邮箱 + 密码（scrypt）。第一个注册的用户接管已有的无主数据。`assistant_id` 是这个人的**助理**（自己的一个 Agent；注册时还没有 Agent 就由网络建一个，否则默认最早创建的），进项目时自动加入、项目会话里陪这个人聊；和它的私聊就是侧栏最上面的“助理”（[assistant.md](assistant.md)） |
| Agent（节点） | 名称、描述、能力、**自身权限**（运行时上限）、是否允许私聊、可选的运行时模型（云侧启用模型目录时是预置模型 id `model_preset`，否则是 provider / model / reasoning_effort；空则用默认）；
运行上限 `timeout_s`（每轮运行超时秒数）与 `max_delegations`（每次被 @ / 每个任务的委托次数），默认都不限制；`skills`（勾上的我的技能名）、`connections`（勾上的我的连接）；**所有者**。Agent 只属于所有者、只为所有者工作（不变量 16），没有 public / shared。系统不管理注入 Agent 上下文的经验 |
| Project（项目） | 成员集合与委托边界，**没有主 Agent**。`internal`（内部项目）：只有所有者，成员只能是所有者自己的 Agent。`joint`（联合项目，参照 Raft 的 joint channel，research: raft#joint-channel）：主持人（`owner_id`）邀请用户，接受后成为参与者（`participants`），每人只能加入自己的 Agent。创建后不能转换类型。人（`people`）进项目时助理自动加入，人在项目里时助理不能移出。委托只能发给项目成员；网络**不会**自己创建 Agent，只接受 Agent 的提议、由人批准。每个项目带一份**项目档案**（见下）。设计见 [docs/projects.md](../projects.md) |
| Conversation | 参照 Raft 的频道与 DM（research: raft#channels-dm）：`project`（项目群聊）**每个项目一个**，建项目即有，人在消息里 **@ 谁，谁接任务**；`session`（项目会话）是一个人在项目里和自己助理的私下对话，可以有多个，只有这个人看得到；`dm`（私聊）**每个 Agent 一个**，只有它的所有者能开，第一次打开时创建，不用 @，不能委托。项目 / Agent 删除后对应对话归档为只读。消息的 `audience` 限定谁看得到这条（如席位失败说明、外溢申请），`private_to` 限定谁看得到它的过程（`steps` / `wake`），`attachments` 是人随消息发的附件（sha / name / size / mime，见 [conversations.md](conversations.md)） |
| Approval | Agent 想在工作目录以外操作时向**它的所有者**发起的一次性申请（pending / allowed / rejected / cancelled） |
| Seat（席位） | 一个 Agent 在一个对话里的执行上下文，id 为 `对话/Agent`：独立 DSH 会话、收件箱、工作目录 `conversations/<conv>/<agent>/`。第一次被 @ 时创建 |
| Task（委托） | Agent ↔ Agent 的一次授权：mission / authority / constraints / expected_outcome / share；带收件箱。`attachments` 是委托方显式转交的对话附件。`owners` 是两端 Agent 的所有者，只有他们看得到这个委托 |
| KnowledgeItem | 任务结果沉淀为知识，key 形如 `<task_id>:result` |
| UserRequest（用户任务） | 人交代给 Agent 的一件**有交付目标**的事，看板上的一张卡。由接任务的 Agent 判断并登记（`open_request`）：闲聊、问答不算。来源消息与下发人由网络从这一轮送达的 @ 它的消息里认定，不信任模型自报。`in_progress → in_review → completed`（或 `cancelled`）；交付（`deliver_request`）前它为此发起的委托必须都有结果；下发人验收或退回（原会话返工）。为它发起的委托记在 `task_ids`，只在任务详情里展示 |
| Proposal | Agent 提议新建的 Agent，由**提议者 Agent 的所有者**审核（`owner_id`），新 Agent 归他；`max_authority` 记录提议者当时的有效权限 |
| 项目档案 | 归项目所有：`brief`（**纲要**：目标、范围、成功标准、当前阶段，≤4000 字，**每轮推送**）、`decisions`（有效 / 被取代 / 撤销，推送最近的有效决定）、文件（`projects/<id>/files/`，每轮只读拷进工作目录的 `project/`，运行后丢弃改动）、`refs`（资源引用：仓库 / 路径 / 文档，只是指针，不授予访问权限；旧版叫 `projects`）、`suggestions`（纲要修改建议与技能提议，`kind` 区分）、`skills`（**项目技能**的元数据，正文在 `projects/<id>/skills/`）、`connector_needs`（**需要的连接器**：只是声明）、`connections_enabled`（联合项目里各 Agent 的所有者打开了哪些自己的连接，只给所有者看）。`archive_rev` 每变一次 +1，席位 / 任务只在版本变化时重新推送 |
| Connection（连接） | 一个人对一个连接器的一次授权（云端，凭证在 `vault/`），或他电脑上某个 AI 工具里已经配好的一个服务（只有名字）。`where` 是 `cloud` 或设备 id，`status` 是 ok / error / missing。不含凭证，只给本人看。见 [connectors-skills.md](connectors-skills.md) |
| 技能 | 一种做法：`SKILL.md`（名字、描述、正文）+ 可选文件。分项目技能、连接器自带的、我的技能、Agent 自己攒的（私有目录里），见 [connectors-skills.md](connectors-skills.md) |
| Draft（草稿） | 助理在助理对话里替主人起草、要发进某个项目群聊的一条消息（`pending → sent / discarded`）。只有主人看得到、能发；发出时以主人的身份、`Message.via = "<助理名> 起草"`，`message_id` 指向发出的那条。助理对话里对应的事件消息带 `draft_id` |
| Schedule（定时） | 一个人的定时：`cron` 或一次性的 `at`，到点往 `agent_id`（他自己的 Agent，通常是助理）的私聊收件箱投一条，带 `title` 与 `instruction`。`created_by` 记是谁建的（人、注册时默认、助理），`failures` / `error` 记送不到的原因。只给主人看 |
| 插件 | 项目装上的一个业务扩展：清单声明的表（记录在 `projects/<id>/plugin-data/`，是项目档案的一部分）、沙箱里的页面、打法（技能）、触发。元数据在 `Project.plugins[name]`（版本、来源、安装人、`disabled`、`data_rev`、各触发的启用人与状态），只有主持人装。触发发的群聊消息、插件页面确认后发的消息带 `Message.via`（插件名）。见 [plugins.md](plugins.md)、不变量 20 |

权限词表：`web.research`、`file.write`、`shell.exec`、`delegate`、`connector.use`（这一轮能用勾给它的连接器）。新增权限的同步点见 AGENTS.md「工作方式」。
