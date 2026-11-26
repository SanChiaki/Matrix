# 业务系统可用的接口（`/api/*`）

给要把 Matrix 接进自己系统的后端开发者：怎么鉴权、哪些接口可以放心用、返回什么、出错怎么认。
想直接照着接“建项目 + 项目会话”，看 [integration-project-sessions.md](integration-project-sessions.md)，那里是按步骤写的。

Matrix 没有单独的“开放接口”：业务系统调的就是网页自己用的 `/api/*`，**每次请求等同某个用户本人**，
他在网页上能做什么、看到什么，接口就能做什么、看到什么（不变量 13）。所以没有应用、令牌、系统账号这些概念。

## 一、鉴权

业务系统的**后端**调 Matrix（不要从浏览器直接调）。两种方式，按部署方在 `login.json` 里开了哪种来：

| 方式 | 请求头 | 什么时候用 |
|---|---|---|
| 共享密钥（临时方案） | `X-Matrix-Key`、`X-Matrix-User`、`X-Matrix-User-Email` | 现在。配置与风险见 [login-driver-authoring.md](login-driver-authoring.md) 第九节 |
| 用户凭据 | `Authorization: Bearer <用户的登录凭据>` | 登录驱动的 `verify` 接好以后，同上第八节 |

两种方式下接口、返回完全一样，以后从密钥换成用户凭据，只换请求头。下面都用共享密钥举例。

### 共享密钥的三个请求头

| 请求头 | 写什么 |
|---|---|
| `X-Matrix-Key` | 部署方发给这个业务系统的密钥（至少 32 个字符）。只放在后端，不进浏览器、前端代码、日志 |
| `X-Matrix-User` | 这次替谁调：这个人在公司里**唯一、不会变**的 user_id（最多 200 个字符）。不要用工号、邮箱这类会换的 |
| `X-Matrix-User-Email` | 这个人的邮箱。**每次都带**即可：还没有 Matrix 账号的人靠它建账号，已有账号的人不看它 |

```bash
curl https://matrix.corp.example/api/me \
  -H "X-Matrix-Key: $EXPENSE_MATRIX_KEY" \
  -H "X-Matrix-User: 8f3a2c01" \
  -H "X-Matrix-User-Email: wang@corp.example"
```

```json
{"id": "user-64f8ac13", "email": "wang@corp.example", "assistant_id": "agent-e4b6ad73",
 "agent_count": 1, "home_id": "conv-f4f7b721", "admin": false}
```

- 第一次替某个人调用时，Matrix 给他建账号和一个助理（一个云侧 Agent）。**邀请别人进项目按邮箱找人，
  被邀请的人要先有账号**：业务系统替他调过任意一次接口（比如 `GET /api/me`）就有了。
- 之后只认 `X-Matrix-User`：邮箱换了不影响，也不会改 Matrix 里记的邮箱。
- 这个邮箱在 Matrix 里已经有账号（比如他以前用邮箱密码注册过）而没有绑定时，默认拒绝（401），由部署方决定是否打开 `link_by_email`。
- `/api/admin/*` 不认这两种方式（403）。
- 反向代理要把这些请求头原样传给 Matrix（[reverse-proxy.md](reverse-proxy.md)）。

### 鉴权出错

| 状态码 | 情况 |
|---|---|
| 401 | 密钥不对、这里没配 `api_keys`、没带 `X-Matrix-User`、标识太长、认不出这个人（没给邮箱建不了账号、邮箱已有账号没绑定、域名不在 `allow_domains`） |
| 403 | 调了 `/api/admin/*` |

## 二、通用约定

- 请求体与返回都是 JSON（`Content-Type: application/json`），UTF-8。上传附件例外：请求体就是文件内容。
- id 是带前缀的字符串：`user-…`、`agent-…`、`prj-…`、`conv-…`、`msg-…`、`req-…`、`task-…`。当不透明字符串存，不要解析。
- 时间是 Unix 秒（浮点数）。
- 返回的对象字段会**增加**，不会无故减少或改含义：按名字取要用的字段，忽略不认识的。
- 写操作不是幂等的（没有幂等键）：超时后重试前先查一下（比如发消息前看对话里是不是已经有了）。
- 没有分页：列表一次返回全部。

### 出错

出错时返回 `{"detail": "<给人看的中文说明>"}`（422 时 `detail` 是字段错误的数组）。

| 状态码 | 含义 | 怎么处理 |
|---|---|---|
| 401 | 没认出是谁 | 见上 |
| 404 | 不存在，**或这个用户看不到**（两者不区分，不变量 13） | 检查 id、检查替的是不是对的人 |
| 409 | 这个操作现在做不了（名字重复、不是成员、不是主持人能做的事的另一些情况、对话进行中等） | `detail` 说明原因，可以直接给用户看 |
| 413 | 附件太大（单个 20 MB） | |
| 422 | 请求体字段不对（缺字段、超长） | 改请求 |

## 三、稳定子集

下面这些是业务系统接入时用得到、我们会尽量保持不变的接口。**不在表里的 `/api/*`（插件、模板、发布、连接器、设备、IM、管理后台等）是给
Matrix 自己的网页用的，会随版本变，不建议依赖**；确实要用时先和 Matrix 的维护者确认。

### 1. 我是谁、我能看到什么

| 接口 | 说明 |
|---|---|
| `GET /api/me` | 这个用户：`id`、`email`、`assistant_id`（他的助理）、`admin` |
| `GET /api/network` | 这个用户看得到的全部：`agents`、`projects`、`conversations`（摘要，不含消息）、`invitations`（等他接受的邀请）、`approvals` / `questions`（等他处理的）、`seq`（事件流当前序号）。数据多时较大，不要轮询它，用事件流 |

### 2. Agent

Agent 属于创建它的用户（所有者），只有所有者能改、能删；别人的 Agent 按不存在处理（404）。

| 接口 | 说明 |
|---|---|
| `POST /api/agents` | 新建。`{name, description, capabilities?, authority?, chat_enabled?, model_preset?, timeout_s?, max_delegations?}` |
| `PATCH /api/agents/{agent_id}` | 修改，只给要改的字段 |
| `DELETE /api/agents/{agent_id}` | 删除（有进行中的工作时 409） |
| `GET /api/models` | 云侧可选的预置模型（`models`，每个有 `id`）；`active` 为 false 时没有启用模型目录 |
| `GET /api/public-agents` | 所有人都能用的公共 Agent（可以拉进任何项目） |

字段：

| 字段 | 说明 |
|---|---|
| `name` | 2–80 个字符，**全站唯一**（重名 409）。会话里 `@名字` 就是它 |
| `description` | 2–2000 个字符。写进它每一轮的提示词：它是谁、负责什么、怎么做，等于它的“设定” |
| `capabilities` | 最多 10 个短语，给人和别的 Agent 看它擅长什么 |
| `authority` | 权限上限，取值：`web.research`（查公开信息）、`file.write`（在工作目录里写文件）、`shell.exec`（执行命令）、`delegate`（委托项目里别的成员）、`connector.use`（用所有者授权的连接器）。不给就都没有：只能对话 |
| `chat_enabled` | 能不能和它私聊，默认 true |
| `model_preset` | 预置模型的 id（`GET /api/models`）；不给用默认模型 |
| `timeout_s` / `max_delegations` | 每轮运行超时（秒）、每次被 @ 最多委托几次；不给不限制 |

返回的 Agent 对象里常用的：`id`、`name`、`description`、`authority`、`owner_id`、`owner_email`、`mine`（是不是这个用户能改的）、
`status`（`idle` / 工作中）、`public`。

### 3. 项目

建项目的人是**主持人**。项目成员是 Agent；每个人只能把**自己的** Agent（或公共 Agent）加进项目。

| 接口 | 谁能做 | 说明 |
|---|---|---|
| `POST /api/projects` | 任何人 | `{name, description?, member_ids?, default_agent?}` |
| `GET /api/projects/{project_id}` | 项目里的人 | 详情：成员、默认回答成员、参与者、档案等 |
| `PATCH /api/projects/{project_id}` | 见右 | `{name?, description?, default_agent?}` 只有主持人；`member_ids?`（整份替换）按下面两行的规则 |
| `POST /api/projects/{project_id}/members` | 项目里的人 | `{agent_id}`：把自己的 Agent 或公共 Agent 加进来 |
| `DELETE /api/projects/{project_id}/members/{agent_id}` | 主持人，或那个 Agent 的所有者 | 移出成员 |
| `DELETE /api/projects/{project_id}` | 主持人 | 删除（有进行中的对话时 409） |

`default_agent`（**项目会话的默认回答成员**）：项目里的一个 Agent，按 id 或名字给。在这个项目里开会话时没另外指定，
没 @ 任何人的消息就交给它。只有主持人能定；要是项目成员（不是就 409）；`PATCH` 时传 `null` 是不设；
换了只影响**之后新开**的会话；它被移出项目时自动清空。返回的项目对象里是 `default_agent_id`。

返回的项目对象里常用的：`id`、`name`、`description`、`member_ids`、`default_agent_id`、`owner_id`（主持人）、
`participants`（别的参与者的用户 id）、`shared`（有没有别人）。

### 4. 参与者：邀请与接受

别的用户要用这个项目，由**主持人逐个邀请**，对方接受后成为参与者（没有“自行加入”）。

| 接口 | 谁能做 | 说明 |
|---|---|---|
| `POST /api/projects/{project_id}/invitations` | 主持人 | `{email}`：对方要已经有 Matrix 账号（否则 409“没有用户”） |
| `POST /api/projects/{project_id}/invitation` | 被邀请的人 | `{accept: true \| false}` |
| `POST /api/projects/{project_id}/leave` | 参与者 | 退出（他的 Agent 一并移出，他的会话变只读） |
| `DELETE /api/projects/{project_id}/participants/{user_id}` | 主持人 | 移出参与者，或撤回还没接受的邀请 |

等某个人接受的邀请在他的 `GET /api/network` 的 `invitations` 里（`project_id`、`name`、`host`）。

### 5. 项目会话

项目会话是**一个人**在项目里的私下对话：只有开它的人看得到，带项目上下文，能 @ 项目里的任何 Agent。一个人在一个项目里可以开很多个。

| 接口 | 说明 |
|---|---|
| `POST /api/projects/{project_id}/sessions` | 开会话。`{title?, agent?}`：`agent` 是这个会话的默认回答成员（项目里的 Agent，id 或名字）；不给就用项目的 `default_agent`，项目也没定时用这个人已在项目里的助理，都没有就没有默认成员 |
| `GET /api/projects/{project_id}/sessions` | 这个人在项目里的会话（摘要），最近的在前 |
| `PATCH /api/sessions/{conv_id}` | `{title?, archived?, agent?}`：改名、归档 / 恢复、换默认回答成员（`agent: null` = 不要默认成员） |
| `DELETE /api/conversations/{conv_id}` | 删除（只能删已归档的） |

注意：这个人在项目里已经有一个**还没发过消息**、默认成员相同的会话时，`POST` 返回的是那一个，不新建。

会话对象（摘要）：`id`、`kind`（`session`）、`project_id`、`agent_id`（默认回答成员，可以是 null）、`title`、`owner_id`、
`status`（`idle` 空闲 / `thinking` 有 Agent 在回答 / `waiting` 在等委托出去的工作）、`archived`、`message_count`、`last_message`、`updated_at`。

### 6. 消息

| 接口 | 说明 |
|---|---|
| `POST /api/conversations/{conv_id}/messages` | 发消息。`{text, mentions?, attachments?}`，返回这条消息（`id`、`mentions` 是实际交给了谁） |
| `GET /api/conversations/{conv_id}` | 会话详情：摘要字段 + `messages`（全部消息）、`members`（能 @ 的 Agent id）、`running`（正在回答的 Agent id）、`questions`（等这个人回答的提问） |
| `POST /api/conversations/{conv_id}/attachments?name=<文件名>` | 上传附件：请求体就是文件内容，类型取 `Content-Type`；返回 `{sha, name, size, mime}`，发消息时放进 `attachments` |
| `GET /api/conversations/{conv_id}/attachments/{sha}` | 下载附件（人传的、Agent 发的都在这） |
| `POST /api/conversations/{conv_id}/seats/{agent_id}/stop` | 打断这个 Agent 正在跑的一轮（只有 Agent 的所有者能） |
| `POST /api/questions/{question_id}` | 回答 Agent 运行中向这个人提的问题：`{answers: [{id, selected?, custom?}], decline?}`。也可以直接在会话里回一条消息（只 @ 它或不 @） |

交给谁：`text` 里写 `@名字`，或在 `mentions` 里给 Agent id（两者可以并用，可以同时 @ 几个）；都没有时交给会话的默认回答成员；
没有默认成员时消息只留在会话里，没人回答。`mentions` 里有不是项目成员的 id 时 409。

发消息的接口**立刻返回**，Agent 的回答是异步的：经事件流（下一节）收，或轮询 `GET /api/conversations/{conv_id}` 直到 `status` 回到 `idle`。

消息对象：

| 字段 | 说明 |
|---|---|
| `id`、`ts` | |
| `role` | `user` 人发的；`agent` Agent 的回答；`system` 某个 Agent 这一轮的通知（比如失败、被打断）；`event` 事件（成员变动等） |
| `text` | 正文（Markdown） |
| `agent_id` | `agent` / `system`：哪个 Agent |
| `user_id` | `user`：哪个用户 |
| `mentions` | `user`：交给了哪些 Agent |
| `attachments` | `[{sha, name, size, mime}]` |
| `steps` | `agent`：这一轮的过程（中间文本、工具调用）；只有这个 Agent 的所有者拿得到，别人是空的 |

### 7. 事件流（SSE）

`GET /api/events?since=<seq>`：一条长连接，`text/event-stream`，只推这个用户看得到的事件。每条：

```
id: 132
data: {"seq": 132, "type": "message.added", "ts": 1795677856.27, "data": {"conversation_id": "conv-…", "message": {…}}}
```

- 每个用户一条连接（请求头里替谁，收到的就是谁的事件）。断了重连时带 `since=<最后收到的 seq>` 或 `Last-Event-ID`，中间的会补发。
- 15 秒没有事件时发一行注释（`: keepalive`）保活；代理不要缓冲这条路径。
- 服务重启后序号从头计：重连后收到的 `seq` 比手里的小是正常的，以对话详情为准对一次。
- 起点可以取 `GET /api/network` 返回的 `seq`。

业务系统用得到的事件：

| `type` | `data` | 说明 |
|---|---|---|
| `message.added` | `conversation_id`、`message` | 对话里多了一条消息：`role=agent` 的就是 Agent 的回答 |
| `conversation.updated` | `conversation`（摘要） | 状态、标题、默认成员等变了：`status` 回到 `idle` 表示这一轮结束 |
| `run.progress` | `context_id`（`<conv_id>/<agent_id>`）等 | Agent 运行中的过程；只给它的所有者，用来显示“正在做什么”，可以不管 |
| `project.updated` | `project` | 项目的成员、默认回答成员、参与者变了 |
| `project.invited` | `project_id`、`user_id`、`invitation` | 这个人收到了邀请 |
| `question.updated` | `question` | Agent 向这个人提了问题，或问题已经回答 |
| `agent.updated` | `agent` | Agent 的资料或状态变了 |

别的事件类型忽略即可。回答不是逐字流式的：一轮结束时整条到（过程在 `run.progress` 里）。

## 四、要知道的限制

- `/api/*` 原本只给 Matrix 自己的网页用。上面的稳定子集改动时会在这份文档和合并说明里写明；别的接口不承诺。
- 没有接口版本号、没有幂等键、没有分页、没有限速（请自行控制并发）。
- 没有回调（webhook）：要实时就订阅事件流。
- 共享密钥方式下 Matrix 不核实后端说的是谁（[login-driver-authoring.md](login-driver-authoring.md) 第九节“要接受的”）。
