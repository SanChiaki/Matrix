# 执行模型与看板

> 入口见 [AGENTS.md](../../AGENTS.md)。

## 执行模型：异步委托 + 收件箱唤醒

参照 Raft 的 inbox + wake 模型（research: raft#inbox-wake）：

- `delegate` **立即返回**任务 id，不阻塞。每个执行上下文（席位、任务）有一个收件箱；收件箱有内容且没在运行时，网络续接它的会话跑一轮。同一上下文同时只跑一轮，运行中到达的内容等这一轮结束再送达。
- 任务状态：`running → in_review → completed`，另有 `failed`（可续接）、`rejected`（授权阶段被拒）。交付进入 `in_review`，交付通知送进委托方收件箱；委托方 `accept_task` 或 `request_changes`（执行者在原会话返工）。送达那一轮结束仍未处理的交付视为接受（`auto_accepted`）；只认送达时的那一版——这一轮里退回、执行者又在本轮结束前重新交付的新版，委托方还没看到，留到下一轮由它自己验收。
- 执行者自己还有进行中的子任务或待审核提议时，这一轮的回复只记为进展（`task.notes`），等回报唤醒后再交付。
- 对话状态：`thinking`（有席位在跑一轮）/ `waiting`（有进行中的委托）/ `idle`。
- 运行上限在 Agent 设置里（`timeout_s`、`max_delegations`，默认不限制）：超时由运行时执行（云侧 dsh 杀进程；端侧随 `run.request.timeout_s`
  下发，连接器的驱动执行），按 `TIMEOUT` 处理。端侧连接断开时，等结果的轮次立刻按传输错误结束（`DeviceRuntime.abort_all`），任务回到等待。
- 人终止一轮（`Network.stop_run`，`POST /api/conversations/<id>/seats/<agent>/stop`、`POST /api/tasks/<id>/stop`）：只有正在跑的
  Agent 的所有者能终止。运行时收到 `RunRequest.cancel` 后结束（云侧 dsh 杀进程；端侧发 `cancel`，连接器的驱动结束 harness），
  这一轮按 `STOPPED` 处理：不自动续接；席位中断、等人“继续”；任务失败、可续接，委托方收到的通知写明是人终止的。会话都保留。
- 任务提示词里的交付要求是通用的（结论、交付了什么、在哪里、怎么确认）；篇幅、形式、依据由委托的输出要求与约束决定，本体不写死领域约束。
- 失败语义：运行时错误按类别处理（`runtime.classify_error`）；`MALFORMED_RESPONSE`、`EMPTY_RESPONSE`、`TIMEOUT` 用同一会话自动续接一次（超时的续接提示收尾）。仍失败时这个**席位**进入 `blocked`（其他席位不受影响），失败消息列出它已完成的委托与已入库知识；人点“继续”或再 @ 它，同一会话从中断处接着做。失败的任务由委托方 `retry_task` 或人在 UI 上续接。

## 运行中提问：ask_user_question

Agent 拿不准时能停下来问，而不是自己猜着做、换一条奇怪的路去做（research: deepseek-harness#user-questions）。

- **运行时**：dsh headless 默认不加载提问工具，问题服务（`user-questions`）在但没有应答者。网络给每一轮挂上提问通道（`runtime.QuestionBridge`）时，
  覆盖层插入 dsh 的 `dsh-tool-ask-user`（阻塞式）和 Matrix 的应答插件 `matrix/dsh_plugins/questions.mjs`（与审批应答者同形）：
  插件把问题 POST 给 `/internal/question`，阻塞到网络给出回答或“没有得到回答”。回答是那次工具调用的结果，不是一条消息（参照 reason-network 的 `ask_user`，
  research: reason-network#ask-user；也是 xmatrix 的“harness 原生提问转卡片”，research: xmatrix#human-in-loop，不另造提问工具）。
  运行时能力 `RuntimeCapabilities.questions` 为真时才挂（云侧 dsh、端侧 dsh 驱动、脚本运行时）；Claude Code / Codex 驱动、没报告的旧连接器不挂，提示词也不提。
- **问谁**（`Network.request_question`）：
  - 席位里问这一轮交代事情的人：私聊、项目会话、助理对话是对话的主人；群聊是这一轮 @ 它的人（没有就是最近一次 @ 它的人），都没有时是所有者。
  - 委托里先问**委托方 Agent**：问题进委托方的收件箱（席位或上一级任务），那一轮的通知列出问题与选项（`protocol.format_question_notice`）。
    委托方用 `answer_question` 回答（一个问题、文字正好是某个选项时按选中），或用 `escalate_question` 转给人（可附 note）。
  - 委托方决定不了才给人：转给委托链起点那个席位该问的人（`Network._task_human`）。委托方**这一轮结束时没回答也没转**、或者这时**跑不起来**
    （席位上一轮失败停着、任务不在进行中、端侧不在线），网络自动转（规则挪进代码，委托方不回也不会把执行者卡死）。
  - 只有问题送到的那个委托方上下文能答或转（按令牌认定，不变量 10）；人只能答交给自己的问题，别人看到的是 404（不变量 13）。
  - 会话读过越界的来源时（不变量 22），问题不给所有者以外的人看：只问提问 Agent 的所有者；委托里同交付一样按项目全部参与者判断。
- **人怎么答**：对话里出现一张只给他看的卡片（选项、多选、自己写），“待我处理”与助理对话的总览也列出来，IM 绑定推一条提醒（不带问题内容）。
  **直接在对话里回复也算回答**：私聊、项目会话里的下一条消息，群聊里不 @ 任何人或只 @ 提问的 Agent 的消息，交给正在等的问题（`Message.answers`），
  不再为它排一轮，下一轮的新消息里也不再送这一条（原来没 @ 就不会唤醒，Agent 在群聊里卡住）。也可以“不回答”。
- **没得到回答**：等待超过 `Limits.question_timeout_s`（默认 60 分钟）、人选“不回答”、提问的那一轮结束（停止、出错）或服务重启，问题作废，
  模型拿到“没有得到回答……不要当成同意”的错误。问答结束后对话里留一条只给回答人与所有者看的记录。
- **限制**：等回答期间提问的那一轮一直占着（和审批一样）；迟到的回答不送回（dsh 的 timed 模式在 headless 里没有送回的路）；
  dsh 的子 Agent（`subagent`）不能提问，问题要写进它的结果。

## 一轮做不完的事：长期目标与后台任务

dsh headless 每轮结束就退出（research: deepseek-harness#headless-tools）：它自己的目标续行驱动跑不起来，后台任务也随进程一起结束。

- **长期目标**（`create_goal` / `update_goal` / `get_goal`）：网络从这三个工具的结果跟踪目标（`Seat.goal` / `Task.goal`：id、目标、phase、上限、
  网络已经续了几轮；`engine._goal_track`，这几个工具的结果在 `RunResult.tool_calls` 里不截断）。这一轮正常结束、目标还是 `active`，
  就往同一个收件箱放一条 `goal` 通知，在**同一会话**里接着跑下一轮（`protocol.format_goal_round`，回复上的唤醒摘要是“目标续行 第 n/m 轮”）。
  dsh 把网络发的消息当作人的直接请求，所以模型可以在续行那一轮直接 `update_goal complete / blocked`（会话续接后目标是“停用”的，不需要 resume）。
  - 停止：模型标记 complete / blocked / paused（人让它停时用 pause）；到上限（目标的 `max_goal_rounds`，没写按 10，最多 30：
    `GOAL_ROUNDS_DEFAULT` / `GOAL_ROUNDS_CAP`），席位里给所有者留一条说明；这一轮失败或被人停止也不再续（席位停着、任务失败，同原来的规则）。
  - 席位里每一轮的回复照常发进对话，人可以随时插话（和下一轮续行一起送达）；委托里中间几轮只记进展（`task.notes`），目标结束那一轮才交付，
    到上限时交付里写明没标记完成。
  - 运行时能力 `RuntimeCapabilities.goals` 为真时提示词才说明（云侧 dsh、端侧 dsh 驱动）；Claude Code / Codex 没有这几个工具。
- **后台任务**：覆盖层总是挂上 `matrix/dsh_plugins/jobs.mjs`。Agent 转为空闲时名下还有在跑的后台任务（`bash` 的 `run_in_background`，
  或超过 60 秒自动转后台的命令），插件追加一条 `<background_jobs>` 消息，让它用 `job_output`（wait: true）收完或 `job_kill`；
  Agent 重新忙起来，headless 的收尾检查就不会退出。每次运行最多提醒 3 次（`runtime.JOBS_NUDGES`），之后照常结束（剩下的任务随进程结束）。
- **限制**：跨轮的后台任务仍不支持（任务只活在一轮里，要等就在这一轮里等完）；目标超过 8 KiB 时事件里的结果会被截断，可能读不出状态而不再续。

## 看板：只有人交代的任务

- 看板（`/api/board`）只列 UserRequest，不列 Agent 之间的委托（委托在任务详情里看）。
- 群聊被 @、项目会话与私聊里的消息都可以成为任务。是不是任务由接任务的 Agent 判断（提示词 `protocol._requests`），登记、交付、退回都写一条 `event` 进对话。
- `open_request` / `deliver_request` / `accept_request` / `reopen_request` 只有席位（项目群聊、项目会话、私聊）有，不需要 `delegate`；委托任务里没有。
- 看得到任务的人：下发的人，以及接任务的 Agent 的所有者（内部项目、私聊里是同一个人）。只有下发的人能验收、退回、取消；也可在本轮送达且 @ 接任务席位的消息里明确验收，由该席位用 `accept_request` 代办（记录消息依据，不开放取消）。
- 已交付、等验收时，下发的人在本轮送达且 @ 接任务席位的消息里提出修改：该席位用 `reopen_request` 退回进行中（同看板上的“退回”：`revisions` +1，修改意见取那条消息原文，记 `reopened_by_message_id`），改完用同一个任务重新交付，不另登记新任务。校验同代验收（本轮送达、下发人本人、@ 当前席位）；是不是修改要求由模型判断。等验收时直接再 `deliver_request` 会被拒，错误里指向 `reopen_request`。
