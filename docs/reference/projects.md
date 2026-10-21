# 项目：当前实现要点

> 入口见 [AGENTS.md](../../AGENTS.md)。设计与分阶段计划见 [docs/projects.md](../projects.md)，不变量见 [invariants.md](invariants.md) 第 18–20 条。


- `Project`：一个所有者、brief、方面（`workstreams`）。内部群组挂进项目并承担一个方面（`Group.project_id` / `workstream_id`，
  一个群组最多属于一个项目）；群组与项目必须同一个所有者（联合群组看主持人）。
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
  负责人必须在内部群组里。负责人自己提交的不重复的交接直接发出。`lead_accept` 再把这些交接引出的任务交给负责人验收（`review_request`，
  `UserRequest.reviewer / reviewed_by_agent`），验收后依赖它的交接照常自动发出。越界的转给人（不变量 20），人的待办里只出现等人处理的。
- **项目决定**（`Project.decisions`）：只有所有者记录，推送给所有挂载群组，和群组决定冲突时以它为准。
- 用户任务的项目归属由网络按所在群组认定（`open_request` 时），不让模型填；群组换方面 / 移出时未结束的任务跟着改，
  已结束的保留历史。私聊里的任务由下发的人手动归类（`assign_request`），联合群组里的任务不能归类。
- 项目只有所有者看得到（无权限按 404）；`project.*` 事件只推给所有者（`event_view`）。
