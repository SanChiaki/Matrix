# 项目、助理与项目会话：当前实现要点

> 入口见 [AGENTS.md](../../AGENTS.md)。设计见 [docs/projects.md](../projects.md)，不变量见 [invariants.md](invariants.md) 第 2、13、16–18 条。

- `Project`：成员集合与委托边界，没有主 Agent；带一个项目群聊（`Conversation.kind = project`）和一份项目档案（[archive.md](archive.md)）。
  `internal` 只有所有者，`joint` 由主持人邀请参与者（[conversations.md](conversations.md)）。
- **助理**（`User.assistant_id`，`Network.assistant_of`）：注册时网络给没有 Agent 的人建一个（`ensure_assistant`，见 [assistant.md](assistant.md)）；
  没指定时是用户第一个创建的 Agent（`Store.default_assistants`，删掉后取下一个），
  `PUT /api/me/assistant` 换。人进项目（创建、接受邀请）时 `_bring_assistant` 把助理加进成员并在群聊记一条事件；
  人在项目里时助理不能移出（`_check_removable`）。换助理不动已有项目，新助理在下次进项目或开会话时加入。
- **项目会话**（`Conversation.kind = session`，`owner_id` 是这个人，`agent_id` 是开会话时的助理）：
  `POST /api/projects/<id>/sessions` 新建（已有项目里助理还不是成员时这时才加入），`GET` 列出自己的，
  `PATCH /api/sessions/<id>` 改名、归档 / 恢复（进行中不能归档；人已不在项目里不能恢复）。只有它的人看得到（不变量 18）；
  人退出 / 被移出时这个人的会话归档为只读（`_drop_participant`）；助理不在项目里了，会话不能再发消息。
- 会话席位和群聊席位一样：消息直接交给助理（不用 @），能委托项目成员、登记用户任务、用档案工具；档案写入照常在项目群聊里记事件。
  此外每轮带一份**项目现状**（`_session_state` / `protocol.format_session_state`：成员、没结束的用户任务、
  最近 `SESSION_MESSAGES` 条群聊公开消息，字节封顶），按 sha1 记在 `Seat.state_seen`，只在变化时重新推送；
  只读工具 `read_chat`、`read_request`（`SESSION_TOOLS`）只在会话里有。读到的一切按会话主人的可见性过滤。
- 用户任务的项目归属由网络按所在对话认定（`open_request` 时），不让模型填；私聊里的任务由下发的人手动归到自己主持的项目
  （`assign_request`，`PUT /api/requests/<id>/project`）。
- 无权限一律按不存在（404）；`project.*` 事件按可见性推送（`event_view`）。
