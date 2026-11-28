# 业务系统接入：建项目、定默认 Agent、进行项目会话

给业务系统的后端开发者，照着做就能接上。两个场景：

1. **建项目**：创建项目并指定对话的默认 Agent；更换默认 Agent；编辑项目里的 Agent。
2. **项目会话**：用户在项目里开会话、发消息、收 Agent 的回答。

每个接口的请求参数、返回字段、错误码见 [api-for-business-systems.md](api-for-business-systems.md) 第三节（按接口逐个列出）；这里只写这两个场景要用的，示例里的返回只摘了关键字段。

以前用“外部应用”（`mat_` 令牌、`/open/v1/*`、`/svc/*`）或“项目服务”接入的，先看文末的[从外部应用、项目服务迁移](#从外部应用项目服务迁移)。

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

## 从外部应用、项目服务迁移

2026-11-26 的四个改动（[SanChiaki/Matrix#110](https://github.com/SanChiaki/Matrix/pull/110)–[#113](https://github.com/SanChiaki/Matrix/pull/113)）
把业务系统的接入方式换掉了，旧的接口已经删除，没有兼容期：

| 改动 | 内容 |
|---|---|
| #110 | 业务系统的后端带着**用户的凭据**直接调 `/api/*`，每次请求等同那个用户本人（之后又加了临时的共享密钥方式，见上文“准备”） |
| #111 | 项目会话不再依赖助理：能 @ 项目里的任何 Agent，开会话时可以指定默认 Agent |
| #112 | 删除**外部应用**：应用登记、应用令牌（`mat_…`）、业务系统账号、`/open/v1/*`、`/svc/*`、开放给应用的 Agent（不变量 30 撤销） |
| #113 | 删除**项目服务**：服务、服务会话、`/api/services*`、`/api/projects/{id}/services*`（不变量 29 撤销） |

### 概念怎么换

| 以前 | 现在 |
|---|---|
| 应用令牌，按 scope（`agents.read`、`projects.write`、`services.publish`、`services.call`）授权 | 没有应用、没有令牌、没有 scope：每次请求**替某个用户**做，能做的就是他在网页上能做的 |
| **业务系统账号**（`<应用 id>@apps.matrix`）当主持人，真人负责人代管 | 一个**真实用户**当主持人（通常是原来的负责人或运营）；系统账号已删除 |
| 代表一个人的令牌 | 直接替那个人调：`X-Matrix-User` 填他的 user_id（或带他的登录凭据） |
| 发布**项目服务**（入口、岗位、范围、收件目录、读者），调用方开**服务会话** | 主持人把终端用户**邀请进项目**，每个人开自己的**项目会话**，@ 项目里的 Agent；没 @ 时交给默认 Agent |
| 服务的入口 Agent | 项目的**默认 Agent**（`default_agent`），每个会话也可以单独指定 |
| 开放给应用的个人 Agent（所有者自动成为参与者） | 公共 Agent，或主持人自己的 Agent；别人的 Agent 要进项目，先邀请它的所有者，由所有者把自己的 Agent 加进来 |
| 系统账号的连接器（负责人代授权） | 每个人用自己授权的连接器；平台 Agent 要用户身份时走调用方授权透传 |
| `/svc` 一次请求 SSE 流式等到做完 | 发消息立即返回；回答在事件流 `GET /api/events` 里整条到（或轮询会话） |
| 服务用量（`…/services/{sid}/usage`） | 没有按项目的用量接口；管理员在管理后台按用户、按模型看用量 |

### 接口对照

| 以前 | 现在 | 说明 |
|---|---|---|
| `GET /open/v1/agents` | `GET /api/network`（这个用户看得到的 Agent）、`GET /api/public-agents` | 没有“开放给应用的 Agent”了 |
| `GET /open/v1/blueprints` | `GET /api/blueprints` | |
| `GET /open/v1/projects`、`GET /open/v1/projects/{id}` | `GET /api/network`、`GET /api/projects/{project_id}` | 现在返回的是这个用户看得到的全部，包括群聊与档案，不再只有配置 |
| `POST /open/v1/projects` `{name, description, brief, members}` | `POST /api/projects` `{name, description, member_ids, default_agent}`，纲要另用 `PUT /api/projects/{id}/brief` `{text}` | `members` 改名 `member_ids`；只能放主持人自己的 Agent 和公共 Agent |
| `POST /open/v1/projects` 带 `{blueprint, params, roles, digest}` | `POST /api/blueprints/{bid}/projects` `{name, params, roles, confirm, digest}` | 要确认的事同网页：第一次 409 返回 `{confirm, digest}`，带上 `digest` 再调 |
| `PATCH /open/v1/projects/{id}` `{description, brief, roles}` | `PATCH /api/projects/{id}`（`name`、`description`、`member_ids`、`default_agent`）；纲要 `PUT …/brief`；角色 `PUT /api/projects/{id}/roles` `{roles}` | |
| `DELETE /open/v1/projects/{id}` | `DELETE /api/projects/{project_id}` | 主持人才能删；以前“只能删经这个应用建的”那条限制没有了 |
| `PUT /open/v1/projects/{id}/members/{agent}` | `POST /api/projects/{project_id}/members` `{agent_id}` | 移出照旧是 `DELETE …/members/{agent_id}` |
| `PUT /open/v1/projects/{id}/files/{path}` | `POST /api/projects/{project_id}/files?name=<路径>`，请求体就是文件内容 | 路径可以带目录 |
| `PATCH /open/v1/projects/{id}/access` `{path, rule}` | `PUT /api/projects/{project_id}/file-access` `{path, roles, agents}` | `roles`、`agents` 都空 = 所有成员可见（以前的 `rule: null`） |
| `POST /open/v1/projects/{id}/skills` | `PUT /api/projects/{project_id}/skills` `{name, description, body}` | |
| `POST /open/v1/projects/{id}/plugins` `{name, digest}` | `POST /api/projects/{project_id}/plugins` `{name, confirm, digest}` | |
| `POST /open/v1/projects/{id}/services`、`POST /api/projects/{id}/services`（发布） | 设好项目的默认 Agent、资料与可见范围，再邀请读者 | 没有单独的“发布”一步，见下面的[按操作对照](#按操作对照服务--项目会话) |
| `PATCH …/services/{sid}`（修改） | `PATCH /api/projects/{id}`、`PATCH /api/agents/{id}`、`PUT …/file-access` 等 | 按改的是哪个字段，见[字段对照](#服务的字段对照) |
| `DELETE …/services/{sid}`（撤回） | 移出参与者，或删项目 | 见按操作对照 |
| `GET /api/projects/{id}/services`（项目里的服务列表） | `GET /api/projects/{project_id}` | 一个项目就是一个“服务” |
| `GET /open/v1/projects/{id}/services/{sid}/usage`、`GET /api/projects/{id}/services/{sid}/usage` | 没有对应接口 | 管理后台按用户、按模型看用量 |
| `GET /svc/agents/{service}` | `GET /api/projects/{project_id}` | 看项目里有哪些 Agent、默认 Agent 是谁 |
| `POST /svc/agents/{service}/chat` `{message, session_id?}` | 第一次：`POST /api/projects/{id}/sessions` 拿 `conv_id`；之后每条：`POST /api/conversations/{conv_id}/messages` `{text}`；回答：`GET /api/events` | 以前的 `session_id` 对应现在的 `conv_id`，同一个会话续接上下文 |
| `POST /svc/sessions/{session_id}/interrupt` | `POST /api/conversations/{conv_id}/seats/{agent_id}/stop` | 要指明打断哪个 Agent |
| `GET /api/services`、`POST /api/services/{id}/sessions`（网页里的“给你用的服务”） | 项目列表与项目会话 | |

### 按操作对照（服务 → 项目会话）

原来“服务”的每个操作，现在怎么做。**谁来调**：发布方的操作替主持人调，调用方的操作替终端用户调。

| 原来的操作 | 现在怎么做 | 接口（替谁调） |
|---|---|---|
| **发布服务** | ① 建项目，把原来的入口设成默认 Agent，原来的岗位加成成员<br>② 放资料、按需收窄可见范围<br>③ 邀请原来的读者 | ① `POST /api/projects` `{name, description, member_ids, default_agent}`（主持人）<br>② `POST /api/projects/{id}/files?name=…`、`PUT /api/projects/{id}/file-access`（主持人）<br>③ `POST /api/projects/{id}/invitations` `{email}`（主持人）+ `POST /api/projects/{id}/invitation` `{accept: true}`（读者本人） |
| **修改服务** | 按改的字段分别改，见下一节；改了下一轮就生效，不用“重新发布” | `PATCH /api/projects/{id}`、`PATCH /api/agents/{id}`、`PUT …/file-access`、`PUT …/roles`（主持人或 Agent 的所有者） |
| **增加读者** | 邀请 + 接受 | 同发布第 ③ 步 |
| **去掉读者** | 移出参与者：他的会话变只读 | `DELETE /api/projects/{project_id}/participants/{user_id}`（主持人） |
| **撤回服务** | 移出所有参与者；或不设默认 Agent（`default_agent: null`）让新消息没人接；或删项目 | `DELETE …/participants/{user_id}`、`PATCH /api/projects/{id}`、`DELETE /api/projects/{id}`（主持人） |
| **看服务名片** | 看项目：成员、默认 Agent | `GET /api/projects/{project_id}`（读者） |
| **列出“给你用的服务”** | 列出这个人参与的项目 | `GET /api/network` 的项目列表（读者） |
| **开服务会话** | 开项目会话 | `POST /api/projects/{project_id}/sessions`（读者），返回 `conv_id` |
| **调用服务**（`/svc/…/chat` 发一条） | 在会话里发消息 | `POST /api/conversations/{conv_id}/messages` `{text}`（读者） |
| **收流式回复**（SSE 的 `delta` / `done`） | 订阅事件，收 `message.added`（整条），看 `conversation.updated` 的 `status` 回到 `idle` | `GET /api/events`，或轮询 `GET /api/conversations/{conv_id}`（读者） |
| **续接会话**（带 `session_id`） | 往同一个 `conv_id` 继续发 | 同上 |
| **中断** | 打断正在回答的那个 Agent | `POST /api/conversations/{conv_id}/seats/{agent_id}/stop`（读者） |
| **读服务会话的历史** | 读会话 | `GET /api/conversations/{conv_id}`（读者） |
| **结束一个服务会话** | 归档 | `PATCH /api/sessions/{conv_id}` `{archived: true}`（读者） |
| **看用量** | 没有按项目的用量接口 | 管理后台 |

### 服务的字段对照

发布服务时填的每个字段，现在落在哪里：

| 服务的字段 | 现在 | 怎么设 |
|---|---|---|
| `name`、`title`、`description` | 项目的名字与说明 | `POST /api/projects`、`PATCH /api/projects/{id}` 的 `name`、`description` |
| `entry`（入口） | 项目的默认 Agent | `default_agent`；某个会话单独指定用 `POST …/sessions` 的 `agent`，开着的会话换用 `PATCH /api/sessions/{conv_id}` `{agent}` |
| `staff`（入口能委托的 Agent） | 项目成员 | `member_ids`，或 `POST …/members` / `DELETE …/members/{agent_id}`。会话里也能直接 @ 它们；入口要能委托它们，入口 Agent 自己要有 `delegate` 权限 |
| `authority`（服务授予的权限，发布时冻结） | 没有这一层：用 Agent 自己的权限 | `PATCH /api/agents/{id}` `{authority}`（所有者）；要比平时更窄，就为这个项目单独建一个 Agent |
| `scope`（能读的资料） | 项目资料与可见范围 | `POST …/files?name=` 放资料，`PUT …/file-access` `{path, roles, agents}` 收窄到某些角色、成员 |
| `inbox`（收件目录） | 没有对应 | Agent 写的东西在它的工作目录里，要发给人用 `send_file`，要进项目档案用档案工具 |
| `plugins`（服务里能用的插件） | 项目插件 | `POST /api/projects/{id}/plugins` |
| `readers`（给谁用） | 项目参与者 | 逐个 `POST …/invitations` + 对方 `POST …/invitation` |
| `public: true`（所有登录用户） | 没有对应 | 业务系统在用户第一次进来时替他做一次邀请 + 接受 |

### 一个服务怎么改成项目会话

以前：主持人发布服务 `intake`（入口 `财务助手`，`readers` 给几个人），业务系统凭令牌调 `/svc/agents/svc-…/chat`。现在：

1. **主持人**（原来的负责人）建项目，把原来的入口设成默认 Agent：
   `POST /api/projects` `{"name": "报销咨询", "member_ids": ["agent-财务助手", "agent-合同审核"], "default_agent": "agent-财务助手"}`。
   原来服务里的资料用 `POST …/files?name=` 放进项目，要收窄 Agent 能看的范围用 `PUT …/file-access`。
2. **每个终端用户**被主持人邀请、替他接受（上文场景二第 1 步）。这对应原来服务的 `readers`；以前的 `public: true`（所有登录用户都能用）
   现在没有：业务系统在用户第一次进这个功能时替他做一次邀请 + 接受。
3. **替用户**开项目会话、发消息、收回答（场景二第 2–4 步）。会话 id 存在业务系统里，下次续接同一个会话。

和服务相比要知道的差别：

- **权限**：没有“服务授予的权限”这一层（发布时冻结、比入口更窄）。Agent 用的是它自己的 `authority`，要收窄就给它更小的权限，或单独建一个只用于这个项目的 Agent。
- **数据范围**：没有服务的 `scope`、收件目录。项目资料默认所有成员可见，用 `file-access` 按角色、成员收窄；Agent 写下的东西照常进它的工作目录、或经档案工具进项目。
- **谁看得到**：项目会话只属于开它的人，主持人和其他参与者看不到（以前的服务会话也是这样）。被 @ 进来的别人的 Agent，它的所有者只看得到经过它的那几轮。
- **过程**：以前 `/svc` 流里有 `progress`（委托、执行方的工具与中途的话）。现在业务系统只收到回答本身（`message.added`）和会话状态（`conversation.updated` 的 `thinking` / `waiting` / `idle`）；
  逐步的过程只推给 Agent 的所有者（`run.progress`）。
- **超时**：以前 `/svc` 一次请求最多等 15 分钟。现在是异步的：发完消息就返回，等事件或轮询，没有这个上限。
- **限频**：以前每把令牌每分钟 600 次。现在没有接口级的限频，业务系统自己控制并发。
- **留痕**：以前档案日志记“经哪个应用”。现在只记是哪个用户做的，还不区分“网页上做的”与“业务系统替他做的”。

### 升级时已有数据怎么处理

升级到这几个版本，启动时自动做，**升级前照常备份**（数据库存储会在首次保存时清掉相应的行）：

| 数据 | 处理 |
|---|---|
| 外部应用、应用令牌 | 丢弃，令牌立刻失效（401） |
| 业务系统账号 | 删除。它主持的项目转给第一个还在的负责人（没有就给第一个参与者，再没有就给登记应用的管理员），其余负责人留作参与者 |
| 系统账号的会话、附件、连接器及保管库里的凭证 | 删除 |
| 已发布的项目服务 | 丢弃 |
| 服务会话（连同附件） | 删除 |
| `state.json` | 先另存一份 `state.pre-services.json` |

### 迁移清单

- [ ] 向部署方要接入方式：用户凭据（`login.json` 的 `api_auth`，[login-driver-authoring.md](login-driver-authoring.md) 第八节），或临时的共享密钥（`api_keys`，第九节）
- [ ] 业务系统里删掉 `mat_` 令牌，不再调 `/open/v1/*`、`/svc/*`；反向代理上去掉为它们做的放行，原样传 `Authorization`、`X-Matrix-*` 请求头（[reverse-proxy.md](reverse-proxy.md)）
- [ ] 确定每个项目的主持人（真实用户），确认升级后项目转到了谁名下
- [ ] 原来的每个服务改成一个项目：成员、默认 Agent、资料与可见范围（上一节）
- [ ] 原来服务的读者改成逐个邀请；业务系统里补上“用户第一次进来时邀请 + 接受”
- [ ] 调用路径改成：开项目会话 → 发消息 → 订阅事件（或轮询），保存 `conv_id` 以便续接
- [ ] 原来依赖系统账号连接器的，改成让每个用户授权自己的连接器
- [ ] 用一个测试用户把场景一、场景二从头走一遍

