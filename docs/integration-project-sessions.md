# 业务系统接入：建项目、定默认 Agent、进行项目会话

给业务系统的后端开发者，照着做就能接上。两个场景：

1. **建项目**：创建项目并指定对话的默认 Agent；更换默认 Agent；编辑项目里的 Agent。
2. **项目会话**：用户在项目里开会话、发消息、收 Agent 的回答。

每个接口的请求参数、返回字段、错误码见 [api-for-business-systems.md](api-for-business-systems.md) 第三节（按接口逐个列出）；这里只写这两个场景要用的，示例里的返回只摘了关键字段。

## 先弄清四件事

- **每次请求都是替某个用户做的**，等同他本人。Matrix 里没有“业务系统”这个身份：项目是某个人建的，会话是某个人开的。
- **项目**里的成员是 Agent。建项目的人是**主持人**；别的用户要用这个项目，由主持人按邮箱**逐个邀请**，对方接受后成为参与者。
- **项目会话**是一个人在项目里的私下对话：只有他自己看得到，能 @ 项目里的任何 Agent。没 @ 时交给**默认 Agent**。
- **Agent 属于创建它的用户**，只有所有者能改它。所以“编辑项目里的 Agent”要替它的所有者调。

最常见的搭法：业务系统里负责配置的人（管理员、运营）当主持人，建 Agent、建项目、定默认 Agent；终端用户被邀请进项目，各自开会话提问。

## 准备

向 Matrix 的部署方要：地址（下面写成 `$MATRIX`）和这个业务系统的密钥（`login.json` 的 `api_keys`，
[login-driver-authoring.md](login-driver-authoring.md) 第九节）。每个请求带三个请求头：

```bash
# 替谁调，就写谁的 user_id 和邮箱。user_id 要是公司里唯一、不会变的那个，不要用工号、邮箱
who() { echo "-H X-Matrix-Key:$EXPENSE_MATRIX_KEY -H X-Matrix-User:$1 -H X-Matrix-User-Email:$2"; }
AS_HOST=$(who 8f3a2c01 wang@corp.example)     # 主持人：王
AS_USER=$(who 27bd90e4 li@corp.example)       # 终端用户：李
J='-H Content-Type:application/json'
```

密钥只放在后端。第一次替某个人调用时，Matrix 自动给他建账号（邮箱每次都带上即可）。

## 场景一：建项目，定默认 Agent

### 1. 建 Agent（替主持人）

```bash
curl -X POST $MATRIX/api/agents $AS_HOST $J -d '{
  "name": "报销助手",
  "description": "你负责回答公司报销制度的问题：先问清楚费用类型和金额，再按制度给出能不能报、要哪些单据。",
  "authority": ["web.research"]
}'
# → {"id": "agent-b5b043fe", "name": "报销助手", "owner_id": "user-64f8ac13", "mine": true, ...}
```

- `name` 全站唯一（重名 409），会话里 `@报销助手` 就是它。
- `description` 就是它的设定，写进它每一轮的提示词。
- `authority` 是它能做什么的上限：`web.research`、`file.write`、`shell.exec`、`delegate`、`connector.use`；不给就只能对话。
- 已经有现成的 Agent（主持人自己的，或公共 Agent：`GET /api/public-agents`）就不用建，拿它的 `id`。

### 2. 建项目并指定默认 Agent（替主持人）

```bash
curl -X POST $MATRIX/api/projects $AS_HOST $J -d '{
  "name": "报销咨询",
  "description": "员工报销问题",
  "member_ids": ["agent-b5b043fe", "agent-25b39643"],
  "default_agent": "agent-b5b043fe"
}'
# → {"id": "prj-2ed74cc5", "member_ids": [...], "default_agent_id": "agent-b5b043fe", "owner_id": "user-64f8ac13", ...}
```

- `member_ids`：项目里的 Agent。只能放主持人自己的 Agent 和公共 Agent。
- `default_agent`：对话的默认 Agent，要在 `member_ids` 里（id 或名字都行）。在这个项目里开的会话，没 @ 任何人的消息都交给它。
- 记下返回的项目 `id`。

### 3. 更换默认 Agent（替主持人）

```bash
curl -X PATCH $MATRIX/api/projects/prj-2ed74cc5 $AS_HOST $J -d '{"default_agent": "agent-25b39643"}'
# → {..., "default_agent_id": "agent-25b39643"}
```

- 只有主持人能换；新的要是项目成员（不是就 409）。传 `null` 是不设默认 Agent。
- **只影响之后新开的会话**。已经开着的会话各自记着自己的默认 Agent；要让某个开着的会话也换，替会话的主人调
  `PATCH /api/sessions/{conv_id}`，`{"agent": "agent-25b39643"}`（见场景二第 5 步）。
- 默认 Agent 被移出项目时自动清空，不会悄悄换成别人。

### 4. 编辑项目里的 Agent

**改 Agent 本身**（名字、设定、权限、模型），替它的所有者调：

```bash
curl -X PATCH $MATRIX/api/agents/agent-b5b043fe $AS_HOST $J -d '{
  "description": "你负责回答公司报销制度的问题。2026 年起差旅标准按新制度执行……",
  "authority": ["web.research", "file.write"]
}'
```

- 只给要改的字段；可改 `name`、`description`、`capabilities`、`authority`、`chat_enabled`、`model_preset`、`timeout_s`、`max_delegations`。
- Agent 不是项目专属的：改了以后，它在所有项目和私聊里都跟着变，下一轮回答就生效。
- 不是它的所有者时返回 404（别人的 Agent 按不存在处理）。

**改项目里有哪些 Agent**，替主持人（或那个 Agent 的所有者）调：

```bash
curl -X POST   $MATRIX/api/projects/prj-2ed74cc5/members $AS_HOST $J -d '{"agent_id": "agent-9c1e77aa"}'   # 加
curl -X DELETE $MATRIX/api/projects/prj-2ed74cc5/members/agent-25b39643 $AS_HOST                             # 移出
```

看项目现在的样子：`GET /api/projects/{project_id}`（`member_ids`、`default_agent_id`、`participants`）。

## 场景二：进行项目会话

### 1. 让用户成为项目的参与者（一次性）

主持人自己可以直接开会话，跳过这一步。别的用户要先被邀请、接受：

```bash
# a. 确保被邀请的人已经有 Matrix 账号：替他调一次任意接口
curl $MATRIX/api/me $AS_USER

# b. 主持人邀请（按邮箱）
curl -X POST $MATRIX/api/projects/prj-2ed74cc5/invitations $AS_HOST $J -d '{"email": "li@corp.example"}'

# c. 替被邀请的人接受
curl -X POST $MATRIX/api/projects/prj-2ed74cc5/invitation $AS_USER $J -d '{"accept": true}'
```

- 邮箱要和这个人 Matrix 账号里的一致（第一次调用时带的 `X-Matrix-User-Email`）。
- 已经是参与者、已经邀请过时 b 返回 409，可以当成功处理。
- 现在没有“自行加入”，也没有批量邀请：有多少用户就做多少次 b + c。业务系统可以在用户第一次进这个功能时做。
- 参与者能看到项目群聊和项目档案；别人的项目会话他看不到。

### 2. 开会话（替用户）

```bash
curl -X POST $MATRIX/api/projects/prj-2ed74cc5/sessions $AS_USER $J -d '{"title": "差旅报销"}'
# → {"id": "conv-fdfe42ba", "kind": "session", "agent_id": "agent-b5b043fe", "title": "差旅报销", "status": "idle", ...}
```

- 返回的 `agent_id` 是这个会话的默认 Agent：没另外指定时就是项目的 `default_agent`。
- 想让这个会话用另一个默认 Agent，加 `"agent": "<项目里的 Agent id 或名字>"`。
- `title` 可以不给：第一条消息发出后按内容自动起。
- 这个用户在项目里已有一个**还没发过消息**、默认 Agent 相同的会话时，返回的是那一个（不会攒下一串空会话）。
- 列出这个用户在项目里的会话：`GET /api/projects/{project_id}/sessions`。

### 3. 订阅事件（替用户，长连接）

```bash
curl -N "$MATRIX/api/events?since=0" $AS_USER
```

```
id: 132
data: {"seq": 132, "type": "message.added", "ts": 1795677856.27,
       "data": {"conversation_id": "conv-fdfe42ba",
                "message": {"id": "msg-4f84b8b6", "role": "agent", "agent_id": "agent-b5b043fe", "text": "差旅报销需要……", ...}}}
```

- 每个在线用户一条连接，推的是他看得到的所有事件；按 `data.conversation_id` 分到各个会话。
- `message.added` 且 `message.role == "agent"`：Agent 的回答（一轮结束时整条到，不是逐字流式）。
- `conversation.updated`：`conversation.status` 是 `thinking`（在回答）/ `waiting`（在等它委托出去的工作）/ `idle`（这一轮结束）。
- 断了重连时带 `since=<最后收到的 seq>`；重连后以 `GET /api/conversations/{conv_id}` 对一次。
- 不想维持长连接：发完消息后每隔一两秒 `GET /api/conversations/{conv_id}`，直到 `status` 回到 `idle`，取新增的消息。

### 4. 发消息（替用户）

```bash
# 没 @：交给默认 Agent
curl -X POST $MATRIX/api/conversations/conv-fdfe42ba/messages $AS_USER $J -d '{"text": "出差的打车费怎么报？"}'
# → {"id": "msg-0b850190", "role": "user", "mentions": ["agent-b5b043fe"], ...}

# @ 项目里的任意 Agent：正文里写 @名字，或在 mentions 里给 id（可以同时 @ 几个）
curl -X POST $MATRIX/api/conversations/conv-fdfe42ba/messages $AS_USER $J -d '{"text": "帮我看下这张单据", "mentions": ["agent-25b39643"]}'
```

- 接口立刻返回；返回里的 `mentions` 是这条消息实际交给了谁。空数组表示没人接（没 @、会话也没有默认 Agent），消息只留在会话里。
- 能 @ 谁：`GET /api/conversations/{conv_id}` 的 `members`（默认 Agent 排第一），名字在 `GET /api/projects/{project_id}` / `GET /api/network` 的 Agent 列表里查。
- 同一个 Agent 还在回答上一条时再发，会排在后面接着答，不会丢。
- 带附件：先 `POST /api/conversations/{conv_id}/attachments?name=发票.pdf`（请求体就是文件内容，单个 ≤20 MB），
  把返回的 `{sha, name, mime}` 放进消息的 `attachments`（一条最多 10 个）。Agent 发回来的文件在回答消息的 `attachments` 里，
  用 `GET /api/conversations/{conv_id}/attachments/{sha}` 下载。

### 5. 读历史、换默认 Agent、收尾（替用户）

```bash
curl $MATRIX/api/conversations/conv-fdfe42ba $AS_USER                 # 全部消息 + status + members + running
curl -X PATCH $MATRIX/api/sessions/conv-fdfe42ba $AS_USER $J -d '{"agent": "审核员"}'     # 换这个会话的默认 Agent（null = 不要）
curl -X PATCH $MATRIX/api/sessions/conv-fdfe42ba $AS_USER $J -d '{"title": "打车费"}'      # 改名
curl -X PATCH $MATRIX/api/sessions/conv-fdfe42ba $AS_USER $J -d '{"archived": true}'      # 归档（变只读；进行中时 409）
```

消息的 `role`：`user` 人发的，`agent` Agent 的回答，`system` 某个 Agent 这一轮的通知（失败、被打断），`event` 事件。界面上一般只展示前两种，`system` 用来提示“这一轮没成功”。

## 会遇到的情况

| 情况 | 表现 | 怎么办 |
|---|---|---|
| Agent 这一轮失败（模型出错、超时） | 会话里多一条 `role=system` 的说明，`status` 回到 `idle` | 让用户再发一条（再 @ 它会在同一会话里接着做），不用重发原需求 |
| Agent 向用户提问（要补充信息才能继续） | 事件 `question.updated`；`GET /api/conversations/{id}` 的 `questions` 里有 | 用户直接在会话里回一条消息就算回答；或 `POST /api/questions/{question_id}` |
| 默认 Agent 被移出项目 | 项目的 `default_agent_id` 变 null；已开的会话没 @ 的消息没人接（返回的 `mentions` 为空） | 主持人重新定默认 Agent，已开的会话 `PATCH` 换一个 |
| 用户被移出项目 / 退出 | 他的会话归档成只读，发消息 409 | |
| 替错了人、id 不对 | 404（看不到和不存在不区分） | 检查 `X-Matrix-User` |
| 别人的 Agent 在用户的会话里被 @ | 它照常回答；它的所有者只看得到经过它的那几轮，看不到会话的其余部分 | 不用处理 |
| 群聊 | 每个项目另有一个所有参与者都看得到的群聊（`POST /api/conversations` `{project_id}` 取到）；群聊里**没有**默认 Agent，只有被 @ 的才回答 | 业务系统一对一问答用项目会话，不要用群聊 |

## 最小流程回顾

```
主持人：POST /api/agents                → agent_id
主持人：POST /api/projects              （member_ids、default_agent）→ project_id
主持人：POST /api/projects/{id}/invitations   （每个终端用户一次）
用户：  POST /api/projects/{id}/invitation    {accept: true}
用户：  POST /api/projects/{id}/sessions      → conv_id
用户：  GET  /api/events                      （长连接，收回答）
用户：  POST /api/conversations/{conv_id}/messages
```
