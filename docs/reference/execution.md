# 执行模型与看板

> 入口见 [AGENTS.md](../../AGENTS.md)。

## 执行模型：异步委托 + 收件箱唤醒

参照 Raft 的 inbox + wake 模型（research: raft#inbox-wake）：

- `delegate` **立即返回**任务 id，不阻塞。每个执行上下文（席位、任务）有一个收件箱；收件箱有内容且没在运行时，网络续接它的会话跑一轮。同一上下文同时只跑一轮，运行中到达的内容等这一轮结束再送达。
- 任务状态：`running → in_review → completed`，另有 `failed`（可续接）、`rejected`（授权阶段被拒）。交付进入 `in_review`，交付通知送进委托方收件箱；委托方 `accept_task` 或 `request_changes`（执行者在原会话返工）。送达那一轮结束仍未处理的交付视为接受（`auto_accepted`）。
- 执行者自己还有进行中的子任务或待审核提议时，这一轮的回复只记为进展（`task.notes`），等回报唤醒后再交付。
- 对话状态：`thinking`（有席位在跑一轮）/ `waiting`（有进行中的委托）/ `idle`。
- 运行上限在 Agent 设置里（`timeout_s`、`max_delegations`，默认不限制）：超时由运行时执行（云侧 dsh 杀进程；端侧随 `run.request.timeout_s`
  下发，连接器的驱动执行），按 `TIMEOUT` 处理。端侧连接断开时，等结果的轮次立刻按传输错误结束（`DeviceRuntime.abort_all`），任务回到等待。
- 人终止一轮（`Network.stop_run`，`POST /api/conversations/<id>/seats/<agent>/stop`、`POST /api/tasks/<id>/stop`）：只有正在跑的
  Agent 的所有者能终止。运行时收到 `RunRequest.cancel` 后结束（云侧 dsh 杀进程；端侧发 `cancel`，连接器的驱动结束 harness），
  这一轮按 `STOPPED` 处理：不自动续接；席位中断、等人“继续”；任务失败、可续接，委托方收到的通知写明是人终止的。会话都保留。
- 任务提示词里的交付要求是通用的（结论、交付了什么、在哪里、怎么确认）；篇幅、形式、依据由委托的输出要求与约束决定，本体不写死领域约束。
- 失败语义：运行时错误按类别处理（`runtime.classify_error`）；`MALFORMED_RESPONSE`、`EMPTY_RESPONSE`、`TIMEOUT` 用同一会话自动续接一次（超时的续接提示收尾）。仍失败时这个**席位**进入 `blocked`（其他席位不受影响），失败消息列出它已完成的委托与已入库知识；人点“继续”或再 @ 它，同一会话从中断处接着做。失败的任务由委托方 `retry_task` 或人在 UI 上续接。

## 看板：只有人交代的任务

- 看板（`/api/board`）只列 UserRequest，不列 Agent 之间的委托（委托在任务详情里看）。
- 群聊被 @、项目会话与私聊里的消息都可以成为任务。是不是任务由接任务的 Agent 判断（提示词 `protocol._requests`），登记、交付、退回都写一条 `event` 进对话。
- `open_request` / `deliver_request` / `accept_request` / `reopen_request` 只有席位（项目群聊、项目会话、私聊）有，不需要 `delegate`；委托任务里没有。
- 看得到任务的人：下发的人，以及接任务的 Agent 的所有者（内部项目、私聊里是同一个人）。只有下发的人能验收、退回、取消；也可在本轮送达且 @ 接任务席位的消息里明确验收，由该席位用 `accept_request` 代办（记录消息依据，不开放取消）。
- 已交付、等验收时，下发的人在本轮送达且 @ 接任务席位的消息里提出修改：该席位用 `reopen_request` 退回进行中（同看板上的“退回”：`revisions` +1，修改意见取那条消息原文，记 `reopened_by_message_id`），改完用同一个任务重新交付，不另登记新任务。校验同代验收（本轮送达、下发人本人、@ 当前席位）；是不是修改要求由模型判断。等验收时直接再 `deliver_request` 会被拒，错误里指向 `reopen_request`。
