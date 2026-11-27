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
- id 是带前缀的字符串：`user-…`、`agent-…`、`prj-…`、`conv-…`、`msg-…`、`req-…`、`task-…`、`dev-…`。当不透明字符串存，不要解析。
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
| 429 | 设备配对相关的数量限制（没用过的配对令牌太多、配对码输错太多） | 稍后再试 |
| 422 | 请求体字段不对（缺字段、超长） | 改请求 |

## 三、接口清单（稳定子集）

下面这些是业务系统接入时用得到、我们会尽量保持不变的接口。**不在这里的 `/api/*`（插件、模板、发布、连接器、IM、管理后台等）是给
Matrix 自己的网页用的，会随版本变，不建议依赖**；确实要用时先和 Matrix 的维护者确认。

| 分组 | 接口 |
|---|---|
| [用户](#1-用户) | `GET /api/me`、`GET /api/network` |
| [Agent](#2-agent) | `POST /api/agents`、`PATCH /api/agents/{agent_id}`、`DELETE /api/agents/{agent_id}`、`GET /api/models`、`GET /api/public-agents` |
| [项目](#3-项目) | `POST /api/projects`、`GET /api/projects/{project_id}`、`PATCH /api/projects/{project_id}`、`DELETE /api/projects/{project_id}`、`POST /api/projects/{project_id}/members`、`DELETE /api/projects/{project_id}/members/{agent_id}` |
| [参与者](#4-参与者邀请与接受) | `POST /api/projects/{project_id}/invitations`、`POST /api/projects/{project_id}/invitation`、`POST /api/projects/{project_id}/leave`、`DELETE /api/projects/{project_id}/participants/{user_id}` |
| [项目会话](#5-项目会话) | `POST /api/projects/{project_id}/sessions`、`GET /api/projects/{project_id}/sessions`、`PATCH /api/sessions/{conv_id}`、`DELETE /api/conversations/{conv_id}` |
| [消息](#6-消息与附件) | `POST /api/conversations/{conv_id}/messages`、`GET /api/conversations/{conv_id}`、`POST /api/conversations/{conv_id}/attachments`、`GET /api/conversations/{conv_id}/attachments/{sha}`、`POST /api/conversations/{conv_id}/seats/{agent_id}/stop`、`POST /api/questions/{question_id}` |
| [事件流](#7-事件流sse) | `GET /api/events` |
| [设备](#8-设备端侧连接器) | `GET /api/devices`、`POST /api/devices/enroll`、`DELETE /api/devices/{device_id}`、`GET /api/devices/pair/{code}`、`POST /api/devices/pair/{code}/approve`、`POST /api/devices/pair/{code}/deny` |

**每个接口都要带的请求头**（下面各节不再重复）：

| 请求头 | 必填 | 说明 |
|---|---|---|
| `X-Matrix-Key` | 是 | 业务系统的密钥 |
| `X-Matrix-User` | 是 | 替谁调：他在公司里唯一、不会变的 user_id |
| `X-Matrix-User-Email` | 建议每次都带 | 他的邮箱；没账号时靠它建 |
| `Content-Type: application/json` | 有请求体时 | 上传附件例外，见那一节 |

**每个接口都可能返回的错误**（下面各节只列它自己特有的）：401（没认出是谁，见第一节）、404（路径里的 id 不存在或这个用户看不到）、
422（请求体字段不对）。

表里的“必填”一栏：**是** = 必须给；**否** = 可以不给，括号里是不给时的取值。类型后面的 `?` 表示可以是 `null`。

### 0. 返回里的对象

几个对象在很多接口里出现，先集中写。只列业务系统用得到的字段；返回里还有别的字段（给 Matrix 网页用的），忽略即可。

#### 用户对象

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 用户 id（`user-…`） |
| `email` | string | Matrix 里记的邮箱（小写） |
| `assistant_id` | string? | 他的助理（一个 Agent）的 id |

#### Agent 对象

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | Agent id（`agent-…`） |
| `name` | string | 名字，全站唯一；会话里 `@名字` 就是它 |
| `description` | string | 设定：写进它每一轮的提示词 |
| `capabilities` | string[] | 擅长什么（短语，存成小写） |
| `authority` | string[] | 权限上限，取值见 `POST /api/agents` |
| `effective_authority` | string[] | 实际生效的权限（它运行的地方做不到的会被去掉） |
| `chat_enabled` | boolean | 能不能和它私聊 |
| `model_preset` | string? | 选的预置模型 id；`null` = 默认模型 |
| `device_runtime` | string? | 绑定的设备 id（`dev-…`，见[第 8 节](#8-设备端侧连接器)）；`null` = 在平台云侧运行 |
| `harness` | string? | 在设备上用哪个 harness（驱动）；`null` = 设备的默认，云侧的 Agent 总是 `null` |
| `timeout_s` | integer? | 每轮运行超时（秒）；`null` = 不限制 |
| `max_delegations` | integer? | 每次被 @ 最多委托几次；`null` = 不限制 |
| `owner_id` | string | 所有者的用户 id |
| `owner_email` | string | 所有者的邮箱 |
| `mine` | boolean | 这次请求替的这个用户能不能改它 |
| `public` | boolean | 是不是公共 Agent |
| `status` | string | `idle` 空闲 / `busy` 正在运行 |
| `created_at` | number | 创建时间 |

#### 项目对象

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 项目 id（`prj-…`） |
| `name` | string | 名字 |
| `description` | string | 描述 |
| `member_ids` | string[] | 项目里的 Agent id |
| `default_agent_id` | string? | 项目会话的默认回答成员；`null` = 没设 |
| `owner_id` | string | 主持人的用户 id |
| `participants` | string[] | 别的参与者的用户 id（不含主持人） |
| `invited` | string[] | 已邀请、还没接受的用户 id（**只有主持人拿得到**） |
| `shared` | boolean | 有没有别人（有参与者或待接受的邀请） |
| `created_at` | number | 创建时间 |

**项目详情**（`GET /api/projects/{project_id}`、邀请与移出参与者的返回）在项目对象之上多这些：

| 字段 | 类型 | 说明 |
|---|---|---|
| `is_host` | boolean | 这个用户是不是主持人 |
| `host` | string | 主持人的邮箱 |
| `participant_emails` | object | `{用户 id: 邮箱}`，主持人和所有参与者 |
| `invited_emails` | object | `{用户 id: 邮箱}`，还没接受的；不是主持人时为 `{}` |

#### 会话对象（摘要）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 会话 id（`conv-…`） |
| `kind` | string | `session` 项目会话（另有 `project` 群聊、`dm` 私聊，本文不涉及） |
| `project_id` | string | 所在项目 |
| `agent_id` | string? | 这个会话的默认回答成员；`null` = 没有 |
| `title` | string | 标题 |
| `owner_id` | string | 开会话的用户 id |
| `status` | string | `idle` 空闲 / `thinking` 有 Agent 在回答 / `waiting` 在等它委托出去的工作 |
| `archived` | boolean | 已归档（只读） |
| `blocked` | boolean | 有 Agent 上一轮失败了、还没接着做 |
| `message_count` | integer | 这个用户看得到的消息数 |
| `last_message` | string | 最后一条消息的前 120 个字符 |
| `created_at` / `updated_at` | number | 创建、最近变动的时间 |

#### 消息对象

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 消息 id（`msg-…`） |
| `role` | string | `user` 人发的；`agent` Agent 的回答；`system` 某个 Agent 这一轮的通知（失败、被打断）；`event` 事件（成员变动等） |
| `text` | string | 正文（Markdown） |
| `agent_id` | string? | `agent` / `system`：哪个 Agent |
| `user_id` | string? | `user`：哪个用户发的 |
| `mentions` | string[] | `user`：这条消息实际交给了哪些 Agent |
| `attachments` | 附件说明[] | 带的文件（人传的、Agent 发的） |
| `steps` | object[] | `agent`：这一轮的过程（中间文本、工具调用）；只有这个 Agent 的所有者拿得到，别人是 `[]` |
| `task_ids` | string[] | `agent`：这一轮里它发起的委托 |
| `answers` | string? | `user`：这条消息回答了哪个提问（提问 id） |
| `ts` | number | 时间 |

#### 附件说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `sha` | string | 内容的 sha256（64 位十六进制），也是下载时用的 id |
| `name` | string | 文件名 |
| `size` | integer | 字节数 |
| `mime` | string | 类型 |

#### 提问对象

Agent 运行中向人提的一批问题（它停下来等回答）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 提问 id |
| `agent_id` / `agent_name` | string | 谁问的 |
| `conversation_id` | string | 在哪个对话里 |
| `target_user_id` | string? | 等谁回答 |
| `items` | object[] | 一批问题：`{id, question, header?, detail?, options: [{label, description?}], multi_select}` |
| `status` | string | `pending` 等回答 / `answered` / `cancelled` |
| `answers` | object[] | 已有的回答：`{id, selected: [选项的 label], custom?}` |
| `created_at` | number | 提问时间 |

### 1. 用户

#### `GET /api/me`：这次请求替的是谁

没有参数。第一次替某个人调用任何接口时 Matrix 会给他建账号；常用这个接口来“确保这个人有账号”。

**返回** 200：用户对象，另有

| 字段 | 类型 | 说明 |
|---|---|---|
| `agent_count` | integer | 他有几个 Agent |
| `home_id` | string? | 他和助理的私聊的会话 id |
| `admin` | boolean | 是不是 Matrix 管理员 |

```bash
curl $MATRIX/api/me -H "X-Matrix-Key: $KEY" -H "X-Matrix-User: 8f3a2c01" -H "X-Matrix-User-Email: wang@corp.example"
```

```json
{"id": "user-64f8ac13", "email": "wang@corp.example", "assistant_id": "agent-e4b6ad73",
 "agent_count": 1, "home_id": "conv-f4f7b721", "admin": false}
```

#### `GET /api/network`：这个用户看得到的全部

没有参数。数据多时返回较大，**不要轮询它**，用事件流。

**返回** 200：

| 字段 | 类型 | 说明 |
|---|---|---|
| `agents` | Agent 对象[] | 他能用的 Agent（自己的、公共的）和他所在项目里的 Agent |
| `projects` | 项目对象[] | 他所在的项目（主持的、参与的）；每项另有 `is_host`、`host_email` |
| `conversations` | 会话对象[] | 他看得到的对话（摘要，不含消息），最近的在前 |
| `invitations` | object[] | 等他接受的邀请：`{project_id, name, description, host}`（`host` 是主持人的邮箱） |
| `questions` | 提问对象[] | 等他回答的提问 |
| `seq` | integer | 事件流当前的序号（可以当 `GET /api/events?since=` 的起点） |

### 2. Agent

Agent 属于创建它的用户（所有者），只有所有者能改、能删；别人的 Agent 按不存在处理（404）。

#### `POST /api/agents`：新建 Agent

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string，2–80 个字符 | 是 | 名字，**全站唯一** |
| `description` | string，2–2000 个字符 | 是 | 设定：它是谁、负责什么、怎么做 |
| `capabilities` | string[]，最多 10 个 | 否（`[]`） | 擅长什么 |
| `authority` | string[] | 否（`[]`：只能对话） | 权限上限，取值：`web.research` 查公开信息；`file.write` 在工作目录里写文件；`shell.exec` 执行命令；`delegate` 委托项目里别的成员；`connector.use` 用所有者授权的连接器 |
| `chat_enabled` | boolean | 否（`true`） | 能不能和它私聊 |
| `model_preset` | string? | 否（`null`：默认模型） | 预置模型的 id，取自 `GET /api/models`；只用于平台云侧的 Agent |
| `device_runtime` | string? | 否（`null`：平台云侧） | 让它在用户自己的某台设备上运行：设备 id，取自 `GET /api/devices`，必须是这个用户的 |
| `harness` | string?，≤40 | 否（`null`：设备的默认） | 设备上的哪个 harness，必须在设备的 `harnesses` 里；不给 `device_runtime` 时不能给 |
| `timeout_s` | integer?，≥1 | 否（`null`：不限制） | 每轮运行超时（秒） |
| `max_delegations` | integer?，≥1 | 否（`null`：不限制） | 每次被 @ 最多委托几次 |

**返回** 200：Agent 对象。

**错误**

| 状态码 | `detail` | 情况 |
|---|---|---|
| 409 | `Agent 名称已存在：…` | 重名 |
| 409 | `未知权限：…` | `authority` 里有不认识的值 |
| 409 | `没有配置预置模型` 等 | `model_preset` 不可用（设备上的 Agent 不能用预置模型） |
| 409 | `设备 … 不存在或属于别人` | `device_runtime` 不是这个用户的设备 |
| 409 | `设备 … 上没有 …` / `未知的 harness：…` / `平台云侧运行内置 dsh，不能选 harness…` | `harness` 不对 |
| 422 | | 缺 `name` / `description`、长度不对 |

```bash
curl -X POST $MATRIX/api/agents $AUTH -H "Content-Type: application/json" -d '{
  "name": "报销助手", "description": "你负责回答公司报销制度的问题。", "authority": ["web.research"]}'
```

```json
{"id": "agent-b5b043fe", "name": "报销助手", "description": "你负责回答公司报销制度的问题。",
 "capabilities": [], "authority": ["web.research"], "effective_authority": ["web.research"],
 "chat_enabled": true, "model_preset": null, "timeout_s": null, "max_delegations": null,
 "owner_id": "user-64f8ac13", "owner_email": "wang@corp.example", "mine": true, "public": false,
 "status": "idle", "created_at": 1795677856.23}
```

（示例里的 `$AUTH` 是上面那三个鉴权请求头，下同。返回只摘了本文列出的字段。）

#### `PATCH /api/agents/{agent_id}`：修改 Agent

只有所有者（或它的维护人）能改。改了以后它在所有项目和私聊里都跟着变，下一轮回答生效。

**路径参数**：`agent_id` Agent id。

**请求体**：只给要改的字段，没给的不动。

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string，2–80 | 新名字（全站唯一） |
| `description` | string，2–2000 | 新设定 |
| `capabilities` | string[] | 整份替换 |
| `authority` | string[] | 整份替换 |
| `chat_enabled` | boolean | |
| `model_preset` | string? | 换预置模型；`null` = 改回默认模型 |
| `device_runtime` | string? | 换到某台设备上运行；`null` = 改回平台云侧（`harness` 一并清掉）。公共 Agent、外部平台的 Agent 不能换到设备上 |
| `harness` | string? | 换设备上的 harness；只给它时沿用原来的设备 |
| `timeout_s` | integer? | `null` = 改回不限制 |
| `max_delegations` | integer? | `null` = 改回不限制 |

**返回** 200：改完的 Agent 对象。

**错误**：404（不是你的 Agent）；409（重名、未知权限、预置模型不可用、设备或 harness 不对，同 `POST /api/agents`）。

```bash
curl -X PATCH $MATRIX/api/agents/agent-b5b043fe $AUTH -H "Content-Type: application/json" \
  -d '{"description": "你负责回答公司报销制度的问题。2026 年起差旅标准按新制度执行。"}'
```

#### `DELETE /api/agents/{agent_id}`：删除 Agent

只有所有者能删。它会从所在的所有项目里移出。

**返回** 200：`{"ok": true}`。

**错误**：404（不是你的）；409 `Agent 正在运行，不能删除`。

#### `GET /api/models`：可选的预置模型

**返回** 200：

| 字段 | 类型 | 说明 |
|---|---|---|
| `active` | boolean | 部署方有没有启用模型目录；`false` 时不用传 `model_preset` |
| `default` | string? | 默认的预置模型 id |
| `models` | object[] | 每个预置模型：`{id, label, description, …}`；`id` 就是 `model_preset` 要填的 |

#### `GET /api/public-agents`：公共 Agent 目录

管理员批准过、所有人都能用的 Agent，可以拉进任何项目。

**返回** 200：数组，每项

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | Agent id |
| `name` / `description` | string | 名字、负责什么 |
| `capabilities` | string[] | |
| `chat_enabled` | boolean | |
| `maintainers` | string[] | 维护人的邮箱 |
| `connectors` | object[] | 它要用的连接器：`{id, name, ready}`（`ready` = 这个用户连上了没有） |

### 3. 项目

建项目的人是**主持人**。项目成员是 Agent；每个人只能把**自己的** Agent（或公共 Agent）加进项目。

#### `POST /api/projects`：建项目

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string，1–80 | 是 | 项目名 |
| `description` | string，≤500 | 否（`""`） | 描述 |
| `member_ids` | string[] | 否（`[]`） | 项目里的 Agent：只能是这个用户自己的，或公共 Agent |
| `default_agent` | string? | 否（`null`：不设） | 项目会话的默认回答成员：`member_ids` 里的一个，给 id 或名字 |

**返回** 200：项目对象（`default_agent_id` 是认定后的 Agent id）。

**错误**

| 状态码 | `detail` | 情况 |
|---|---|---|
| 404 | `unknown agent …` | `member_ids` 里有不存在的、或不是这个用户能用的 Agent |
| 409 | `… 不是项目「…」的成员` | `default_agent` 不在 `member_ids` 里 |

```bash
curl -X POST $MATRIX/api/projects $AUTH -H "Content-Type: application/json" -d '{
  "name": "报销咨询", "member_ids": ["agent-b5b043fe", "agent-25b39643"], "default_agent": "agent-b5b043fe"}'
```

```json
{"id": "prj-2ed74cc5", "name": "报销咨询", "description": "",
 "member_ids": ["agent-b5b043fe", "agent-25b39643"], "default_agent_id": "agent-b5b043fe",
 "owner_id": "user-64f8ac13", "participants": [], "invited": [], "shared": false, "created_at": 1795677856.24}
```

#### `GET /api/projects/{project_id}`：项目详情

项目里的人（主持人、参与者）能看。

**返回** 200：项目详情（见“返回里的对象”）。

#### `PATCH /api/projects/{project_id}`：改项目、换默认回答成员

**请求体**：只给要改的字段。

| 字段 | 类型 | 谁能改 | 说明 |
|---|---|---|---|
| `name` | string，1–80 | 主持人 | 改名 |
| `description` | string，≤500 | 主持人 | 改描述 |
| `default_agent` | string? | 主持人 | 换默认回答成员（项目里的 Agent，id 或名字）；`null` = 不设。**只影响之后新开的会话** |
| `member_ids` | string[] | 见说明 | 成员列表**整份替换**：新加的要是这个用户自己的或公共 Agent；去掉的要是这个用户的 Agent，或这个用户是主持人。一般用下面两个成员接口更省事 |

`member_ids` 和 `default_agent` 一起给时，先改成员再认默认回答成员。默认回答成员被移出项目时自动清空（变 `null`），不会悄悄换成别人。

**返回** 200：改完的项目对象。

**错误**

| 状态码 | `detail` | 情况 |
|---|---|---|
| 409 | `只有主持人能这样做` | 参与者改 `name` / `description` / `default_agent` |
| 409 | `… 不是项目「…」的成员` | `default_agent` 不是项目成员 |
| 409 | `… 不是你的 Agent，只有它的所有者或主持人能把它移出` | `member_ids` 里去掉了别人的 Agent |

```bash
curl -X PATCH $MATRIX/api/projects/prj-2ed74cc5 $AUTH -H "Content-Type: application/json" \
  -d '{"default_agent": "agent-25b39643"}'
# → {..., "default_agent_id": "agent-25b39643"}
```

#### `DELETE /api/projects/{project_id}`：删除项目

只有主持人能删。

**返回** 200：`{"ok": true}`。

**错误**：409 `项目内有进行中的对话，不能删除`；409 `只有主持人能这样做`。

#### `POST /api/projects/{project_id}/members`：加成员

项目里的人把自己的 Agent 或公共 Agent 加进来。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `agent_id` | string | 是 | 要加的 Agent |

**返回** 200：项目对象。

**错误**：404 `unknown agent …`（不存在或不是这个用户能用的）；409 `… 已经在项目里`。

#### `DELETE /api/projects/{project_id}/members/{agent_id}`：移出成员

主持人能移出任何成员；参与者只能移出自己的 Agent。移出的是默认回答成员时，项目的 `default_agent_id` 变 `null`。

**返回** 200：项目对象。

**错误**：409 `… 不在项目里`；409 `… 不是你的 Agent，只有它的所有者或主持人能把它移出`。

### 4. 参与者：邀请与接受

别的用户要用这个项目，由**主持人逐个邀请**，对方接受后成为参与者（没有“自行加入”、没有批量邀请）。

#### `POST /api/projects/{project_id}/invitations`：邀请（主持人）

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `email` | string，3–200 | 是 | 被邀请的人在 Matrix 里的邮箱。**他要已经有账号**（替他调过一次任意接口就有） |

**返回** 200：项目详情（`invited` 里多了他）。

**错误**

| 状态码 | `detail` | 情况 |
|---|---|---|
| 409 | `只有主持人能这样做` | 调用的人是参与者，不是主持人（不在项目里的人是 404） |
| 409 | `没有用户 …` | 这个邮箱还没有 Matrix 账号 |
| 409 | `已经邀请过 …` | 已邀请、对方还没接受：可以当成功处理 |
| 409 | `… 已经是参与者` | 可以当成功处理 |

#### `POST /api/projects/{project_id}/invitation`：接受或拒绝（被邀请的人）

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `accept` | boolean | 是 | `true` 接受，`false` 拒绝 |

**返回** 200：接受时是项目详情；拒绝时是 `{"ok": true}`。

**错误**：404 `unknown invitation …`（没有等这个人接受的邀请：没邀请过、已经处理过、被撤回了）。

等某个人接受的邀请在他的 `GET /api/network` 的 `invitations` 里，或订阅事件 `project.invited`。

#### `POST /api/projects/{project_id}/leave`：退出（参与者）

没有请求体。他的 Agent 一并移出项目，他在这个项目里的会话变成只读。

**返回** 200：`{"ok": true}`。

**错误**：409 `主持人不能退出；不再需要这个项目可以删除它`。

#### `DELETE /api/projects/{project_id}/participants/{user_id}`：移出参与者、撤回邀请（主持人）

**路径参数**：`user_id` 要移出的参与者，或还没接受邀请的那个人的用户 id。

**返回** 200：项目详情。

**错误**：409 `只有主持人能这样做`；409 `不是这个项目的参与者`（那个人不是参与者、也没有被邀请）。

### 5. 项目会话

项目会话是**一个人**在项目里的私下对话：只有开它的人看得到，带项目上下文，能 @ 项目里的任何 Agent。一个人在一个项目里可以开很多个。

#### `POST /api/projects/{project_id}/sessions`：开会话

项目里的人（主持人、参与者）都能开。

**请求体**（可以是 `{}`）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | string，≤120 | 否（`"新会话"`，发第一条消息后按内容自动起） | 标题 |
| `agent` | string? | 否 | 这个会话的默认回答成员：项目里的一个 Agent，给 id 或名字。不给时依次取：项目的 `default_agent_id` → 这个用户已在项目里的助理 → 没有（`null`） |

注意：这个用户在项目里已经有一个**还没发过消息**、默认成员相同、没归档的会话时，返回的是那一个，不新建。

**返回** 200：会话对象（`agent_id` 是认定后的默认回答成员）。

**错误**：409 `… 不是项目「…」的成员`（`agent` 不在项目里）。

```bash
curl -X POST $MATRIX/api/projects/prj-2ed74cc5/sessions $AUTH -H "Content-Type: application/json" -d '{"title": "差旅报销"}'
```

```json
{"id": "conv-fdfe42ba", "kind": "session", "project_id": "prj-2ed74cc5", "agent_id": "agent-b5b043fe",
 "title": "差旅报销", "owner_id": "user-05730ea0", "status": "idle", "archived": false, "blocked": false,
 "message_count": 0, "last_message": "", "created_at": 1795677856.26, "updated_at": 1795677856.26}
```

#### `GET /api/projects/{project_id}/sessions`：我在这个项目里的会话

**返回** 200：会话对象数组（含已归档的），最近变动的在前。只有这个用户自己的。

#### `PATCH /api/sessions/{conv_id}`：改名、归档、换默认回答成员

只有会话的主人能调。

**请求体**：只给要改的字段。

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | string，≤120 | 改名 |
| `archived` | boolean | `true` 归档（变只读）；`false` 恢复 |
| `agent` | string? | 换这个会话的默认回答成员（项目里的 Agent，id 或名字）；`null` = 不要默认成员。只管之后没 @ 的消息交给谁 |

**返回** 200：改完的会话对象。

**错误**

| 状态码 | `detail` | 情况 |
|---|---|---|
| 409 | `… 不是项目「…」的成员` | `agent` 不在项目里 |
| 409 | `会话进行中或还有进行中的委托，不能归档` | |
| 409 | `你已不在这个项目里，会话只读` | 已退出 / 被移出项目的人恢复会话、换默认成员 |

```bash
curl -X PATCH $MATRIX/api/sessions/conv-fdfe42ba $AUTH -H "Content-Type: application/json" -d '{"agent": "审核员"}'
# → {..., "agent_id": "agent-25b39643"}
```

#### `DELETE /api/conversations/{conv_id}`：删除会话

只能删**已归档**的。

**返回** 200：`{"ok": true}`。

**错误**：409（还没归档）。

### 6. 消息与附件

#### `POST /api/conversations/{conv_id}/messages`：发消息

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string，≤8000 | 有附件时可以是 `""` | 正文。里面的 `@名字` 会被认成 @ 那个成员 |
| `mentions` | string[]，最多 20 | 否（`[]`） | 明确交给哪些成员（Agent id）；和正文里的 `@名字` 取并集 |
| `attachments` | object[]，最多 10 | 否（`[]`） | 先上传得到的附件：`{sha, name, mime?}`（把上传接口的返回原样放进来即可） |

交给谁：正文里 @ 到的 + `mentions` 里的；都没有时交给会话的默认回答成员（`agent_id`）；没有默认成员时消息只留在会话里，没人回答。

**返回** 200：**立刻返回**这条消息，Agent 的回答是异步的。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 消息 id |
| `role` | string | `user` |
| `text` | string | 正文 |
| `mentions` | string[] | 实际交给了哪些 Agent；`[]` = 没人接 |
| `attachments` | 附件说明[] | |
| `ts` | number | 时间 |

**错误**

| 状态码 | `detail` | 情况 |
|---|---|---|
| 409 | `… 不是项目「…」的成员` | `mentions` 里有不是项目成员的 id |
| 409 | `这个对话已归档…，只读` | 会话已归档 |
| 409 | `你已不在这个项目里，会话只读` | |
| 409 | `附件 … 不存在：先上传到这个对话` | `attachments` 里的 `sha` 没在这个对话上传过 |
| 422 | `消息不能为空` | `text` 是空白且没有附件 |

```bash
curl -X POST $MATRIX/api/conversations/conv-fdfe42ba/messages $AUTH -H "Content-Type: application/json" \
  -d '{"text": "出差的打车费怎么报？"}'
```

```json
{"id": "msg-0b850190", "role": "user", "text": "出差的打车费怎么报？",
 "mentions": ["agent-b5b043fe"], "attachments": [], "ts": 1795677856.27}
```

收回答：订阅事件流（第 7 节）等 `message.added`，或轮询下面的详情接口直到 `status` 回到 `idle`。
同一个 Agent 还在回答上一条时再发，新消息排在后面接着答，不会丢。

#### `GET /api/conversations/{conv_id}`：会话详情（含全部消息）

**返回** 200：会话对象的全部字段，另有

| 字段 | 类型 | 说明 |
|---|---|---|
| `messages` | 消息对象[] | 这个用户看得到的全部消息，按时间先后 |
| `members` | string[] | 这个会话里能 @ 的 Agent id（默认回答成员排第一） |
| `running` | string[] | 这一刻正在回答的 Agent id |
| `questions` | 提问对象[] | 这个会话里等这个用户回答的提问；每项另有 `can_answer` |
| `senders` | object | `{用户 id: 邮箱}`：消息里出现过的发送人 |

```json
{"id": "conv-fdfe42ba", "kind": "session", "project_id": "prj-2ed74cc5", "agent_id": "agent-b5b043fe",
 "title": "出差的打车费怎么报？", "status": "idle", "archived": false,
 "members": ["agent-b5b043fe", "agent-25b39643"], "running": [], "questions": [],
 "messages": [
   {"id": "msg-0b850190", "role": "user", "text": "出差的打车费怎么报？", "user_id": "user-05730ea0",
    "agent_id": null, "mentions": ["agent-b5b043fe"], "attachments": [], "steps": [], "ts": 1795677856.27},
   {"id": "msg-4f84b8b6", "role": "agent", "text": "打车费凭发票和行程单报销……", "user_id": null,
    "agent_id": "agent-b5b043fe", "mentions": [], "attachments": [], "steps": [], "ts": 1795677861.02}
 ]}
```

#### `POST /api/conversations/{conv_id}/attachments`：上传附件

先上传，再把返回放进消息的 `attachments`。

**查询参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string，1–200 | 是 | 文件名（要 URL 编码） |

**请求头**：`Content-Type` 写文件的类型（如 `application/pdf`），会记成附件的 `mime`。

**请求体**：文件内容本身（不是 JSON、不是 multipart）。单个最多 20 MB。

**返回** 200：附件说明 `{sha, name, size, mime}`。

**错误**：413 `附件超过上限 20 MB`；409（会话已归档、超出存储配额）。

```bash
curl -X POST "$MATRIX/api/conversations/conv-fdfe42ba/attachments?name=invoice.pdf" $AUTH \
  -H "Content-Type: application/pdf" --data-binary @invoice.pdf
```

```json
{"sha": "fc022288f3e49d09353987de1c32fc1427bd2a8b3657a340e86efb8e34efe003",
 "name": "invoice.pdf", "size": 48213, "mime": "application/pdf"}
```

#### `GET /api/conversations/{conv_id}/attachments/{sha}`：下载附件

人传的、Agent 发回来的文件都从这里取（`sha` 来自消息的 `attachments`）。

**返回** 200：文件内容；`Content-Type` 是附件的 `mime`，`Content-Disposition: attachment; filename="…"`。

**错误**：404（这个对话里没有这个附件，或看不到这个对话）。

#### `POST /api/conversations/{conv_id}/seats/{agent_id}/stop`：打断 Agent 正在跑的一轮

只有这个 Agent 的所有者能调（终端用户打断不了别人的 Agent）。没有请求体。

**返回** 200：`{"ok": true}`；会话里会多一条 `role=system` 的消息说明这一轮被终止。

**错误**：409 `只有这个 Agent 的所有者能终止它的运行`；404（它这时没有在这个会话里运行）。

#### `POST /api/questions/{question_id}`：回答 Agent 的提问

Agent 运行中向人提了问题（提问对象，`status=pending`）时，它停下来等。**最简单的回答方式是直接在会话里发一条消息**
（不 @ 任何人，或只 @ 它）：那条消息就算回答。要按选项回答时用这个接口。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `answers` | object[]，最多 20 | 否（`[]`） | 每个问题一项，见下 |
| `decline` | boolean | 否（`false`） | `true` = 不回答：Agent 拿到“没有得到回答”，自己决定怎么继续 |
| `note` | string，≤500 | 否 | 不回答时的说明 |

`answers` 的每一项：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 问题的 id（提问对象 `items[].id`） |
| `selected` | string[] | 否 | 选中的选项（`items[].options[].label`） |
| `custom` | string，≤4000 | 否 | 自己写的回答 |

**返回** 200：提问对象（`status` 变成 `answered` 或 `cancelled`）。

**错误**：404（不是交给这个人的提问）；409 `这个问题已经回答过了` / `…作废了`。

### 7. 事件流（SSE）

#### `GET /api/events`：订阅这个用户看得到的事件

一条长连接，`text/event-stream`。

**查询参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `since` | integer | 否（`0`：从服务还留着的最早一条开始补） | 从这个序号之后开始推。断线重连时填最后收到的 `seq`；只想要新的，先取 `GET /api/network` 的 `seq` |

**请求头**：也认标准的 `Last-Event-ID`（有它时优先于 `since`）。

**返回** 200：事件一条一条到，每条：

```
id: 132
data: {"seq": 132, "type": "message.added", "ts": 1795677856.27, "data": {"conversation_id": "conv-…", "message": {…}}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | integer | 序号，递增 |
| `type` | string | 事件类型，见下表 |
| `ts` | number | 时间 |
| `data` | object | 随类型而定 |

- 每个用户一条连接（请求头里替谁，收到的就是谁的事件）。
- 15 秒没有事件时发一行注释（`: keepalive`）保活；代理不要缓冲这条路径。
- 服务重启后序号从头计：重连后收到的 `seq` 比手里的小是正常的，以 `GET /api/conversations/{conv_id}` 对一次。
- 回答不是逐字流式的：一轮结束时整条到。

业务系统用得到的事件（别的类型忽略即可）：

| `type` | `data` 的字段 | 说明 |
|---|---|---|
| `message.added` | `conversation_id`、`message`（消息对象） | 对话里多了一条消息；`message.role == "agent"` 就是 Agent 的回答 |
| `conversation.updated` | `conversation`（会话对象） | 状态、标题、默认成员等变了；`status` 回到 `idle` 表示这一轮结束 |
| `conversation.deleted` | `conversation_id` | 会话被删了 |
| `run.progress` | `context_id`（`<conv_id>/<agent_id>`）、`agent_id` 等 | Agent 运行中的过程；只给它的所有者，用来显示“正在做什么”，可以不管 |
| `project.updated` | `project`（项目对象） | 项目的成员、默认回答成员、参与者变了 |
| `project.invited` | `project_id`、`user_id`、`invitation` | 这个人收到了邀请 |
| `project.deleted` | `project_id` | 项目被删了 |
| `question.updated` | `question`（提问对象） | Agent 向这个人提了问题，或问题已经回答 / 作废 |
| `agent.updated` | `agent`（Agent 对象） | Agent 的资料或状态变了 |
| `device.connected` | `device_id`、`runtime` | 这个用户的一台设备连上了（只给设备的所有者） |
| `device.disconnected` | `device_id` | 这个用户的一台设备断开了 |

```bash
curl -N "$MATRIX/api/events?since=131" $AUTH
```

### 8. 设备（端侧连接器）

设备是用户自己电脑上装的连接器 `matrix-connect`，Agent 绑到设备上以后就在那台电脑上、用那里的 harness 运行。
设备属于签发或批准它的那个用户，别人的设备按不存在处理（404）。整体设计见 [device-runtimes.md](device-runtimes.md) 第 2 节，
业务系统想让用户“点一下就连上、建好 Agent”时按 [device-driver-authoring.md](device-driver-authoring.md) 接。

接入设备有两条路，都是让**这个用户**成为设备的所有者：

| 方式 | 流程 | 什么时候用 |
|---|---|---|
| 一次性配对令牌（推荐） | 业务系统后端 `POST /api/devices/enroll` 签一条令牌 → 交给用户的电脑 → 电脑上运行 `matrix-connect setup <服务器> --code <code>` | 业务系统能把令牌安全地送到那台电脑上（比如它自己的桌面端） |
| 设备码配对 | 电脑上运行 `matrix-connect setup <服务器>`，显示配对码 → 用户在 Matrix 网页（或业务系统自己的批准页，调下面的 `pair/{code}` 接口）核对后批准 | 送不了令牌，或者要用户当面确认 |

两种方式都可以顺带一份 Agent 规格（`agent`），配对成功时建好这个 Agent、绑到这台设备上。

**不要**让业务系统后端调 `POST /api/devices` 拿长期设备令牌再转交：那条路能跑通，但长期凭据会经过业务后端和中间通道。
一次性令牌只能兑换一次、10 分钟就过期。

#### 设备对象

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 设备 id（`dev-…`），绑 Agent 时填进 `device_runtime` |
| `name` | string | 设备名 |
| `hostname` | string | 连接器报告的主机名，只用于展示 |
| `harnesses` | string[] | 最近一次报告的 harness，第一个是默认；设备离线时仍按它选 |
| `online` | boolean | 现在连着没有 |
| `revoked` | boolean | 吊销了没有 |
| `last_seen_at` | number? | 最近一次连上的时间；没连过为 `null` |
| `created_at` | number | 创建时间 |

#### 配对时的 Agent 规格

`POST /api/devices/enroll` 和配对申请里的 `agent` 字段，配对成功时按它建 Agent，所有者是这个用户。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string，2–80 | 是 | 名字。已被占用时自动加所有者邮箱的前缀，再重名加序号；实际的名字看兑换结果里的 `agent_name` |
| `description` | string，2–2000 | 是 | 设定 |
| `capabilities` | string[]，最多 10 个 | 否（`[]`） | 同 `POST /api/agents` |
| `authority` | string[] | 否（`[]`） | 同 `POST /api/agents` |
| `chat_enabled` | boolean | 否（`true`） | |
| `harness` | string?，≤40 | 否（`null`：设备的默认） | 设备上的哪个 harness，必须是服务器认得的 |
| `timeout_s` | integer?，≥1 | 否（`null`） | |
| `max_delegations` | integer?，≥1 | 否（`null`） | |

#### `GET /api/devices`：这个用户的设备

**返回** 200：设备对象的数组（含已吊销的，看 `revoked`）。

```bash
curl $MATRIX/api/devices $AUTH
```

```json
[{"id": "dev-3c1a9e20", "name": "王工的电脑", "hostname": "wang-mbp", "harnesses": ["biz-agent", "claude"],
  "online": true, "revoked": false, "last_seen_at": 1795677900.12, "created_at": 1795677850.01}]
```

#### `POST /api/devices/enroll`：签一次性配对令牌

签令牌的这个用户就是将来设备和 Agent 的所有者。令牌 10 分钟内有效、只能兑换一次，每人同时最多 16 条没用过的；
只存在服务器内存里，服务重启后没用过的作废，重新签一条即可。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string，≤80 | 否（`""`：用连接器报告的） | 设备名 |
| `agent` | object? | 否（`null`：只配对，不建 Agent） | Agent 规格，见上 |

**返回** 200：`{"code": "<令牌>", "expires_in": 600}`。`code` 只在这里出现一次，不要写进日志、不要长期存。

**错误**

| 状态码 | `detail` | 情况 |
|---|---|---|
| 400 | `未知的 harness：…` | `agent.harness` 服务器不认得 |
| 429 | `没用过的配对令牌太多，请稍后再试` | 这个用户同时有 16 条没用过的 |

```bash
curl -X POST $MATRIX/api/devices/enroll $AUTH -H "Content-Type: application/json" -d '{
  "name": "王工的电脑",
  "agent": {"name": "报销助手", "description": "在这台电脑上处理报销单据", "harness": "biz-agent"}}'
```

```json
{"code": "…43 个字符…", "expires_in": 600}
```

之后把 `code` 和 Matrix 的地址交给那台电脑，由它运行 `matrix-connect setup https://matrix.corp.example --code <code>`。
兑换成功后，设备会出现在 `GET /api/devices` 里；建了 Agent 的话，还会收到 `agent.updated` 事件，设备连上时收到 `device.connected`。

#### `DELETE /api/devices/{device_id}`：吊销设备

令牌立即失效，在线的连接随之断开。绑在它上面的 Agent 不会被删，只是没有地方运行，可以用 `PATCH /api/agents/{agent_id}` 改回平台云侧或换一台设备。

**返回** 200：`{"ok": true}`。

**错误**：400 `设备不存在`（不存在或不是这个用户的）。

#### `GET /api/devices/pair/{code}`：查看设备码配对申请

业务系统想用自己的页面替用户批准时用。`code` 是电脑上显示的 8 位配对码（`ABCD-EFGH`，大小写、连字符都可以不管）。

**返回** 200：

| 字段 | 类型 | 说明 |
|---|---|---|
| `user_code` | string | 配对码（`ABCD-EFGH`） |
| `name` | string | 连接器报告的设备名 |
| `hostname` | string | 主机名 |
| `harnesses` | string[] | 这台电脑上检测到的 harness |
| `agent` | object? | 申请里附带的 Agent 规格（只有 `name`、`description`、`harness`、`authority`）；没有为 `null` |
| `created_at`、`expires_at` | number | 申请时间、过期时间（申请 10 分钟有效） |

**错误**：404 `配对码不存在或已过期`；429 `输错次数太多，请稍后再试`（每个用户 10 分钟内最多输错 10 次，之后正确的码也不受理）。

#### `POST /api/devices/pair/{code}/approve`：批准

批准的这个用户就是设备的所有者。申请里附带了 Agent 规格的话，同时建好这个 Agent、绑到这台设备上；建不成时设备不会建、申请留在等待中。

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string，≤80 | 否（`""`：用连接器报告的） | 设备名 |

**返回** 200：设备对象（`online` 为 `false`，连接器拿到令牌后才会连上），另加 `agent_id`、`agent_name`（建了 Agent 时，否则为 `null`）。

**错误**：同 `GET /api/devices/pair/{code}`；409（建 Agent 失败，`detail` 说明原因）。

#### `POST /api/devices/pair/{code}/deny`：拒绝

**返回** 200：`{"ok": true}`。连接器下一次轮询时得到 `denied`，什么都不建。

**错误**：同 `GET /api/devices/pair/{code}`。

#### 连接器自己调的接口（业务系统一般用不到）

这些接口不需要登录（电脑上还没有任何凭据），由 `matrix-connect` 调用，列在这里方便排查问题。
如果业务系统自己的桌面端程序不想经过命令行，可以直接调 `POST /api/devices/enroll/redeem`，再按 `matrix/connector/config.py` 的格式写配置、拉起 `matrix-connect run`，一般没必要。

| 接口 | 作用 |
|---|---|
| `POST /api/devices/enroll/redeem` | 用一次性令牌换设备令牌。请求 `{"code", "name", "hostname", "harnesses", "agent"?}`（签令牌时指定了 Agent 的话以签的为准），返回 `{"status": "approved", "token", "device_id", "device_name", "email", "agent_id", "agent_name"}`。令牌不对、用过或过期时返回 404；指定的 harness 这台电脑上没有时返回 400，令牌不作废，装好后可以再兑换 |
| `POST /api/devices/pair` | 发起设备码配对。请求 `{"name", "hostname", "harnesses", "agent"?}`，返回 `{"device_code", "user_code", "expires_in": 600, "interval": 2, "verify_path": "/#pair=<user_code>"}`。等待中的申请全站最多 64 条，超过返回 429 |
| `POST /api/devices/pair/poll` | 拿 `device_code` 轮询，返回 `status`：`pending` / `denied` / `expired` / `approved`。`approved` 时带上 `token`、`device_id`、`device_name`、`email`、`agent_id`、`agent_name`，令牌只交付一次 |
| `WebSocket /api/devices/connect?token=<设备令牌>` | 连接器从设备往外连到服务器的长连接，令牌不对时以 1008 关闭 |
| `GET /connect/install.sh`、`GET /connect/install.ps1`、`GET /connect/dist/{asset}` | 连接器的安装脚本与安装包 |

`POST /api/devices` 也能签设备令牌（返回设备对象加 `token`），但它签的是长期凭据，业务系统不要用，原因见本节开头。

## 四、要知道的限制

- `/api/*` 原本只给 Matrix 自己的网页用。上面的稳定子集改动时会在这份文档和合并说明里写明；别的接口不承诺。
- 没有接口版本号、没有幂等键、没有分页、没有限速（请自行控制并发）。
- 没有回调（webhook）：要实时就订阅事件流。
- 共享密钥方式下 Matrix 不核实后端说的是谁（[login-driver-authoring.md](login-driver-authoring.md) 第九节“要接受的”）。
