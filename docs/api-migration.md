# 接口搬迁：旧接口、新接口的完整参数与映射

外部应用（`mat_` 令牌、`/open/v1/*`、`/svc/*`）和项目服务（`/api/services*`、`/api/projects/{id}/services*`）已经移除，
业务系统改调 `/api/*`，每次请求**替某个用户**做。本文按接口逐个列出：旧接口的完整参数与返回、对应的新接口的完整参数与返回、
字段怎么一一对上、换的时候要注意什么。

- 为什么这么改、概念怎么换、整个流程怎么走：[integration-project-sessions.md](integration-project-sessions.md)（从“概念怎么换”起）。
- 新接口的鉴权、通用约定、全部可用接口：[api-for-business-systems.md](api-for-business-systems.md)。本文只写搬迁会碰到的那些。

表里的写法：**位置**是 `路径` / `查询` / `体`（JSON 请求体）/ `原始体`（请求体就是文件内容）；长度是字符数，列表写条数；
“必填”只标必须给的，其余都可省。新接口一栏的“替谁调”指请求头里的那个用户（见下一节）。

## 一、鉴权与通用约定

| 项 | 旧（`/open/v1`、`/svc`） | 新（`/api/*`） | 换法 |
|---|---|---|---|
| 身份 | `Authorization: Bearer mat_…`：应用令牌，代表应用（系统账号）或授权它的那个人 | 替用户调：`X-Matrix-Key` + `X-Matrix-User` + `X-Matrix-User-Email`（共享密钥），或 `Authorization: Bearer <用户的登录凭据>` | 删掉令牌；每次请求按“这一步替谁做”填用户（见各节的“替谁调”） |
| 授权 | 令牌的 scope：`agents.read`、`projects.write`、`services.publish`、`services.call`，再叠加授权人本来的权限、令牌限定的项目 | 没有 scope：这个用户在网页上能做什么，接口就能做什么 | 原来靠 scope 挡住的操作，现在靠“替谁调”挡：只替有权限的人调 |
| 主持人 | 业务系统账号（`<应用 id>@apps.matrix`） | 一个真实用户 | 升级时项目已转给原来的负责人（[integration-project-sessions.md](integration-project-sessions.md)“升级时已有数据怎么处理”） |
| 限频 | 每把令牌每分钟 600 次，超了 429 | 没有接口级限频 | 业务系统自己控制并发 |
| 返回范围 | 只给配置与用量，不给对话内容（自己调用服务的会话除外） | 这个用户看得到的全部 | 不要把返回原样透给别人，按“替谁调”的那个人的视角用 |
| 错误码 | 401 令牌无效 / 撤销 / 应用停用；403 scope 不够或超出令牌的项目；404 不存在；409 业务冲突；409 带 `{confirm, digest}` 要确认；422 参数不对；429 太频繁 | 401 没认出是谁；403 只用于 `/api/admin/*`；404 不存在或这个用户看不到（`detail` 以 `unknown ` 开头）；409 权限不够或业务冲突（`detail` 是原因）；409 带 `{detail, confirm, digest}` 要确认；413 文件太大；422 参数不对 | “权限不够”从 403 变成 409，按 `detail` 区分；多了 413；没有 429 |
| 要确认的操作 | 第一次调返回 409 `{confirm, digest}`，**原样再调一次并带上 `digest`** 即执行 | 第一次调返回 409 `{detail, confirm, digest}`，**再调一次并带上 `confirm: true`**（建议同时带回 `digest`，期间内容变了会再要求确认） | 多传 `confirm: true`；只带 `digest` 不会执行 |

## 二、查

### `GET /open/v1/agents` → `GET /api/network`、`GET /api/public-agents`

旧：scope `agents.read`，没有参数。返回应用能用的 Agent 名片列表：

| 返回字段 | 含义 |
|---|---|
| `id`、`name`、`description`、`capabilities`、`authority` | 名片 |
| `kind` | `public`（公共 Agent）、`mine`（授权人自己的）、`opened`（所有者开放给这个应用的） |
| `owner` | 所有者邮箱 |
| `connectors` | 服务面要用的连接器 |
| `where`、`platform`、`online` | 跑在哪（云侧 / 端侧 / 外部平台）、哪个平台、端侧在不在线 |

新：替用户调，没有参数。

- `GET /api/network` 的 `agents`：这个用户看得到的 Agent（自己的、公共的、和他同在项目里的）。
- `GET /api/public-agents`：只列公共 Agent，返回 `{id, name, description, capabilities, note, connectors: [{id, name, ready}], chat_enabled, maintainers, mine}`。

| 旧字段 | 新字段 | 说明 |
|---|---|---|
| `id`、`name`、`description`、`capabilities`、`authority` | 同名 | `/api/network` 里另有 `effective_authority`（实际生效的权限） |
| `kind: public` | `public: true`（`/api/network`），或出现在 `/api/public-agents` 里 | |
| `kind: mine` | `mine: true` | |
| `kind: opened` | 没有对应 | 别人的个人 Agent 要进项目，先邀请它的所有者，由所有者把它加进来 |
| `owner` | `owner_email`（另有 `owner_id`） | |
| `connectors` | `/api/public-agents` 的 `connectors`（多了 `name`、`ready`：这个用户连上没有） | 只有公共 Agent 有 |
| `where`、`platform` | `device_runtime`（端侧）、`remote`（外部平台） | 都没有就是云侧 |
| `online` | `device_online` | 只对端侧有意义 |

### `GET /open/v1/blueprints` → `GET /api/blueprints`

旧：scope `agents.read`，没有参数，返回 `[{id, title, description, roles, plugins, params}]`。
新：替用户调，没有参数，返回这个用户能用的模板，字段同名（`id`、`title`、`description`、`roles`、`plugins`、`params`，另有发布者、版本等）。
要看一个模板的完整内容用 `GET /api/blueprints/{bid}`。

## 三、组：项目、成员、资料

### `GET /open/v1/projects`、`GET /open/v1/projects/{id}` → `GET /api/network`、`GET /api/projects/{project_id}`

旧：scope `projects.write` 或 `services.publish`。返回 `{id, name, description, brief, members, participants, roles, files, skills, plugins, services}`，不含对话内容。

新：替参与者调。列表在 `GET /api/network` 的 `projects`；详情 `GET /api/projects/{project_id}`。

| 旧字段 | 新字段 | 说明 |
|---|---|---|
| `id`、`name`、`description`、`roles`、`files`、`skills`、`plugins` | 同名 | 新的 `files` 带目录层级，另有 `folders`；`skill_list`、`plugin_list` 是带说明的列表 |
| `brief` | `brief` | |
| `members` | `member_ids` | |
| `participants` | `participants`（user_id）、`participant_emails` | |
| `services` | 没有 | 一个项目就是原来的一个服务 |
| — | `default_agent_id` | 新增：没 @ 时回答的成员 |
| — | `file_access` | 新增：资料的可见范围（旧的只在 `PATCH …/access` 里写，读不回来） |
| — | 群聊、项目会话 | 新返回包含这个用户看得到的对话 |

### `POST /open/v1/projects`（不带模板）→ `POST /api/projects` + `PUT /api/projects/{id}/brief`

旧：scope `projects.write`，主持人是系统账号（或授权人）。

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `name` | 体 | 字符串 1–80 | 是 | 项目名 |
| `description` | 体 | 字符串 ≤2000 | | 说明 |
| `brief` | 体 | 字符串 ≤4000 | | 纲要 |
| `members` | 体 | 字符串列表 ≤50 条 | | 成员 Agent id：公共的、授权人自己的、开放给这个应用的 |
| `blueprint`、`params`、`roles`、`digest` | 体 | | | 用模板建，见下一小节 |

返回：项目视图（同上一节）。

新：替主持人调，分两步。

`POST /api/projects`：

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `name` | 体 | 字符串 1–80 | 是 | 项目名 |
| `description` | 体 | 字符串 ≤500 | | 说明 |
| `member_ids` | 体 | 字符串列表 | | 成员 Agent id：只能是主持人自己的和公共的 |
| `default_agent` | 体 | 字符串 ≤100，`member_ids` 里的一个（id 或名字） | | 项目会话里没 @ 时回答的成员 |

`PUT /api/projects/{project_id}/brief`：

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `project_id` | 路径 | | 是 | 上一步返回的 `id` |
| `text` | 体 | 字符串 ≤4000 | | 纲要全文（覆盖） |

返回：都是项目详情（`id`、`member_ids`、`default_agent_id`、`owner_id`、`brief`……）。

| 旧参数 | 新参数 | 转换 |
|---|---|---|
| `name` | `name` | 不变 |
| `description` | `description` | 上限 2000 → 500，超出的部分挪进纲要或项目资料 |
| `brief` | `PUT …/brief` 的 `text` | 建完项目再写 |
| `members` | `member_ids` | 改名；“开放给应用的 Agent”不能直接加，要先邀请它的所有者，由所有者用 `POST …/members` 加 |
| — | `default_agent` | 新增。原来服务的入口 Agent 填在这里 |

### `POST /open/v1/projects`（带模板）→ `POST /api/blueprints/{bid}/projects`

旧：同一个接口，带 `blueprint`。

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `name` | 体 | 字符串 1–80 | 是 | 项目名 |
| `blueprint` | 体 | 字符串 ≤100 | 是 | 模板 id |
| `params` | 体 | 对象（任意值） | | 模板参数 |
| `roles` | 体 | 对象 `{角色: Agent id}` | | 每个角色由哪个 Agent 担任 |
| `digest` | 体 | 字符串 ≤200 | | 确认时带回第一次 409 给的指纹 |
| `description`、`brief`、`members` | 体 | 同上 | | 同时写进项目 |

新：替主持人调。

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `bid` | 路径 | | 是 | 模板 id |
| `name` | 体 | 字符串 ≤60 | | 项目名；不给用模板标题 |
| `params` | 体 | 对象 `{参数名: 字符串}` | 模板要求的必填 | 模板参数，值只收字符串 |
| `roles` | 体 | 对象 `{角色: {"agent": Agent id}}` 或 `{角色: {"name": 新 Agent 名}}` | | 选已有 Agent 担任（只能是自己的或项目里的成员）；给 `name` 或不给这个角色 = 按模板新建一个 Agent |
| `parts` | 体 | 对象 | | 只装模板的一部分（不给 = 全装） |
| `project_id` | 体 | 字符串 | | 装进已有项目而不是新建 |
| `confirm` | 体 | 布尔 | 确认时 | `true` = 我看过了，执行 |
| `digest` | 体 | 字符串 ≤64 | | 带回 409 给的指纹；模板期间变了会再要求确认 |

返回：项目详情；要确认时 409 `{detail, confirm: [要确认的事], digest}`。

| 旧参数 | 新参数 | 转换 |
|---|---|---|
| `blueprint` | 路径 `bid` | 从请求体挪到路径 |
| `name` | `name` | 上限 80 → 60 |
| `params` | `params` | 值转成字符串（数字、布尔写成 `"3"`、`"true"`） |
| `roles: {"审核": "agent-1"}` | `roles: {"审核": {"agent": "agent-1"}}` | 包一层 `{"agent": …}` |
| `digest`（再调一次即执行） | `confirm: true` + `digest` | 确认方式变了，见第一节 |
| `description`、`brief`、`members` | 没有 | 建完后再 `PATCH /api/projects/{id}`、`PUT …/brief` |

### `PATCH /open/v1/projects/{id}` → `PATCH /api/projects/{id}`、`PUT …/brief`、`PUT …/roles`

旧：scope `projects.write`。

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `id` | 路径 | | 是 | 项目 |
| `description` | 体 | 字符串 ≤2000 | | 说明 |
| `brief` | 体 | 字符串 ≤4000 | | 纲要 |
| `roles` | 体 | 对象 `{角色键: {name, description, members}}` | | 整体覆盖角色 |

新：替主持人调，拆成三个接口，只传要改的那个。

| 新接口 | 参数（位置：体） | 类型与限制 | 含义 |
|---|---|---|---|
| `PATCH /api/projects/{project_id}` | `name` | 字符串 1–80 | 改名（旧接口不能改名） |
| | `description` | 字符串 ≤500 | 说明 |
| | `member_ids` | 字符串列表 | 整体覆盖成员 |
| | `default_agent` | 字符串 ≤100；显式传 `null` = 不设 | 默认回答的成员 |
| `PUT /api/projects/{project_id}/brief` | `text` | 字符串 ≤4000 | 纲要全文 |
| `PUT /api/projects/{project_id}/roles` | `roles` | 对象 `{角色键: {name, description, members}}` | 整体覆盖角色 |

| 旧参数 | 新位置 | 转换 |
|---|---|---|
| `description` | `PATCH /api/projects/{id}` 的 `description` | 上限 2000 → 500 |
| `brief` | `PUT …/brief` 的 `text` | |
| `roles` | `PUT …/roles` 的 `roles` | 结构不变 |

### `DELETE /open/v1/projects/{id}` → `DELETE /api/projects/{project_id}`

旧：scope `projects.write`，只能删经这个应用建的项目，返回 `{ok: true}`。
新：替主持人调，没有其他参数，能删他主持的任何项目。

### `PUT` / `DELETE /open/v1/projects/{id}/members/{agent}` → `POST /api/projects/{id}/members`、`DELETE …/members/{agent_id}`

| | 旧 | 新 |
|---|---|---|
| 加 | `PUT /open/v1/projects/{id}/members/{agent}`，Agent id 在路径 | `POST /api/projects/{project_id}/members`，体 `{agent_id}`（必填） |
| 移出 | `DELETE /open/v1/projects/{id}/members/{agent}` | `DELETE /api/projects/{project_id}/members/{agent_id}` |
| 替谁调 | 令牌（`projects.write`） | 主持人，或那个 Agent 的所有者 |
| 能加谁 | 公共的、授权人自己的、开放给应用的 | 主持人自己的、公共的；别人的 Agent 由它的所有者（先成为参与者）来加 |
| 返回 | 项目视图 | 项目详情 |

### `PUT /open/v1/projects/{id}/files/{path}` → `POST /api/projects/{project_id}/files?name=`

| 参数 | 旧 | 新 |
|---|---|---|
| 资料路径 | 路径 `{path}` | 查询 `name`（可带目录，如 `制度/差旅.md`） |
| 内容 | 原始体，≤ 项目资料上限 | 原始体，≤ 同一个上限；也可以 JSON 体 `{name, content}` 传文本 |
| 太大 | 413 | 413 |
| 替谁调 | 令牌（`projects.write`） | 主持人 |

### `PATCH /open/v1/projects/{id}/access` → `PUT /api/projects/{project_id}/file-access`

旧：

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `path` | 体 | 字符串 1–500 | 是 | 资料或目录 |
| `rule` | 体 | `{roles?: [...], agents?: [...]}` 或 `null` | | 谁能看；`null` = 所有成员 |

新（替主持人调）：

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `path` | 体 | 字符串 1–300 | 是 | 资料或目录 |
| `roles` | 体 | 字符串列表 ≤20 条 | | 哪些角色能看 |
| `agents` | 体 | 字符串列表 ≤50 条 | | 哪些成员能看 |

| 旧参数 | 新参数 | 转换 |
|---|---|---|
| `path` | `path` | 上限 500 → 300 |
| `rule.roles` | `roles` | 提到顶层 |
| `rule.agents` | `agents` | 提到顶层 |
| `rule: null` | `roles: []`、`agents: []` | 两个都空 = 所有成员可见 |

### `POST /open/v1/projects/{id}/skills` → `PUT /api/projects/{project_id}/skills`

| 参数（体） | 旧限制 | 新限制 | 转换 |
|---|---|---|---|
| `name` | 1–64，必填 | 1–64，必填 | 不变 |
| `description` | 1–1000，必填 | 1–500，必填 | 超过 500 的挪进 `body` |
| `body` | 1–100000，必填 | ≤64000，可空 | 超过 64000 的拆成技能附带的文件（`POST …/skills/{name}/files`） |

方法从 `POST` 改成 `PUT`（同名覆盖）。替主持人调。

### `POST /open/v1/projects/{id}/plugins` → `POST /api/projects/{project_id}/plugins`

| 参数（体） | 旧 | 新 | 转换 |
|---|---|---|---|
| `name` | 1–64，必填：部署方插件目录里的名字 | ≤64，必填，同义 | 不变 |
| `digest` | ≤200：再调一次即执行 | ≤64：带回 409 给的指纹 | 见下一行 |
| `confirm` | — | 布尔：`true` = 执行 | 要确认时第二次调传 `confirm: true` + `digest` |

替主持人调。返回项目详情，要确认时 409 `{detail, confirm, digest}`。

## 四、发：项目服务

服务这一层没有了，下面每个接口都映射到“把项目本身配好 + 邀请读者”。字段落点的总表见 [integration-project-sessions.md](integration-project-sessions.md)“服务的字段对照”，这里给参数级的完整对照。

### `POST /open/v1/projects/{id}/services`、`POST /api/projects/{id}/services` → 多个接口

旧：`/open/v1` 要 scope `services.publish`；网页那个由项目负责人调。两者参数相同：

| 参数（体） | 类型与限制 | 必填 | 含义 |
|---|---|---|---|
| `name` | 字符串 1–64 | 是 | 服务的短名（不可改） |
| `title` | 字符串 1–80 | 是 | 显示名 |
| `description` | 字符串 ≤2000 | | 说明 |
| `entry` | 字符串 1–200 | 是 | 入口 Agent（id 或名字） |
| `staff` | 字符串列表 ≤50 条 | | 入口能委托的成员 |
| `scope` | 字符串列表 ≤50 条 | | 服务里能读的项目资料（路径） |
| `inbox` | 字符串 ≤500 | | 收件目录：服务写下的东西放这里 |
| `authority` | 字符串列表 ≤20 条 | | 服务授予的权限，发布时冻结，不超过入口自身 |
| `plugins` | 字符串列表 ≤30 条 | | 服务里能用的项目插件 |
| `readers` | 字符串列表 ≤200 条 | | 能用的人（邮箱） |
| `public` | 布尔 | | 所有登录用户都能用 |

返回服务视图：`id, name, title, description, version, status, project_id, project_name, entry {id, name}, paused, passes [{agent, owner}], published_at`；
参与者另看得到 `staff, staff_names, scope, inbox, authority, public, plugins, readers, who, sessions`。

新：没有一个接口对应，按字段拆到下面这些接口：

| 旧参数 | 新接口（替谁调） | 新参数 | 转换 |
|---|---|---|---|
| `name`、`title` | `POST /api/projects` / `PATCH /api/projects/{id}`（主持人） | `name` | 两个合成一个，取 `title`（1–80） |
| `description` | 同上 | `description` | 上限 2000 → 500 |
| `entry` | 同上 | `default_agent` | 同义；某个会话单独指定用 `POST …/sessions` 的 `agent` |
| `staff` | 同上 | `member_ids`（含 `entry`） | 入口要能委托它们，入口 Agent 自己要有 `delegate` 权限 |
| `scope` | `POST /api/projects/{id}/files?name=`、`PUT /api/projects/{id}/file-access`（主持人） | `name`；`path`、`roles`、`agents` | 资料默认所有成员可见，要收窄就逐条设可见范围 |
| `inbox` | 没有 | — | Agent 写的东西在它的工作目录里；发给人用 `send_file`，进项目档案用档案工具 |
| `authority` | `PATCH /api/agents/{id}`（Agent 的所有者） | `authority` | 没有“发布时冻结”：用 Agent 自己的权限；要更窄就单独建一个 Agent 给这个项目用 |
| `plugins` | `POST /api/projects/{id}/plugins`（主持人） | `name`、`confirm`、`digest` | 装在项目上，项目里都能用 |
| `readers` | `POST /api/projects/{id}/invitations`（主持人）+ `POST /api/projects/{id}/invitation`（读者本人） | `email`（3–200）；`accept: true` | 逐个邀请，对方接受了才算进来 |
| `public` | 没有 | — | 业务系统在用户第一次进来时替他做一次邀请 + 接受 |

| 旧返回字段 | 新的去处 |
|---|---|
| `id` | 项目 `id`（同时也是原来的服务 id 该存的地方） |
| `version`、`published_at`、`status`、`paused` | 没有：改了就生效，没有版本 |
| `project_id`、`project_name` | 项目 `id`、`name` |
| `entry {id, name}` | `default_agent_id` |
| `passes [{agent, owner}]` | 没有：别人的 Agent 由所有者自己加进项目 |
| `staff`、`staff_names` | `member_ids` |
| `readers`、`who` | `participants`、`participant_emails` |
| `sessions` | 每个人在 `GET /api/network` 里看到自己的项目会话 |

### `PATCH /open/v1/projects/{id}/services/{sid}`、`PATCH /api/projects/{id}/services/{sid}` → 同上各接口

旧参数：除 `name` 外与发布相同，全部可省（`title` 1–80、`description` ≤2000、`entry` 1–200、`staff` ≤50、`scope` ≤50、`inbox` ≤500、`authority` ≤20、`plugins` ≤30、`readers` ≤200、`public`），改了切新版本，下一轮生效。
新：按上表改对应的接口，只传要改的；没有版本，下一轮就生效。`readers` 的增删改成邀请与移出（`DELETE /api/projects/{project_id}/participants/{user_id}`）。

### `DELETE /open/v1/projects/{id}/services/{sid}`、`DELETE /api/projects/{id}/services/{sid}` → 按需选一个

旧：没有参数，撤回后已有服务会话变只读。新（都替主持人调，没有请求体）：

| 想要的效果 | 新接口 |
|---|---|
| 某个人不能再用（他的会话变只读） | `DELETE /api/projects/{project_id}/participants/{user_id}` |
| 会话里没 @ 时不再有人回答 | `PATCH /api/projects/{project_id}` `{"default_agent": null}` |
| 整个不要了 | `DELETE /api/projects/{project_id}` |

### `GET /open/v1/projects/{id}/services/{sid}/usage`、`GET /api/projects/{id}/services/{sid}/usage` → 没有

旧参数：查询 `days`（1–90，默认 30）。返回 `{days, sessions, runs, failed, tokens {input, output, cached, total}, users [{user, runs, failed, tools, tokens, last_at}]}`。
新：没有按项目的用量接口。管理员在管理后台按用户、按模型看；公共 Agent 的维护人看 `GET /api/agents/{agent_id}/public/usage?days=`（1–90，默认 30）。

### `GET /api/projects/{id}/services` → `GET /api/projects/{project_id}`

旧：列项目里的服务。新：一个项目就是一个服务，看项目详情即可。

## 五、调：服务会话与对话

### `GET /svc/agents/{service}` → `GET /api/projects/{project_id}`

旧：scope `services.call`，返回 `{name, description}`。
新：替读者调，取项目详情里的 `name`、`description`；要知道谁回答看 `default_agent_id`、`member_ids`。

### `POST /svc/agents/{service}/chat` → `POST /api/projects/{id}/sessions` + `POST /api/conversations/{conv_id}/messages` + `GET /api/events`

旧：scope `services.call`。

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `service` | 路径 | | 是 | 服务 id |
| `message` | 体 | 字符串 1–20000 | 是 | 这一轮的话 |
| `session_id` | 体 | 字符串 ≤100 | | 续接；不给 = 开新会话 |

返回：同一个请求里的 SSE 流（最多等 15 分钟，安静时每 15 秒一条心跳），见第六节。

新：替读者调，拆成三步。

**1. 开会话**（第一次，之后复用）`POST /api/projects/{project_id}/sessions`：

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `project_id` | 路径 | | 是 | 原来的服务对应的项目 |
| `title` | 体 | 字符串 ≤120 | | 会话标题 |
| `agent` | 体 | 字符串 ≤100 | | 这个会话没 @ 时由谁回答；不给 = 项目的默认 Agent |

返回会话视图：`{id, kind: "session", project_id, agent_id, title, status, messages, …}`，`id` 就是 `conv_id`。

**2. 发消息** `POST /api/conversations/{conv_id}/messages`：

| 参数 | 位置 | 类型与限制 | 必填 | 含义 |
|---|---|---|---|---|
| `conv_id` | 路径 | | 是 | 上一步的 `id` |
| `text` | 体 | 字符串 ≤8000 | `text`、`attachments` 至少一个 | 这一轮的话；正文里的 `@名字` 会识别 |
| `mentions` | 体 | 字符串列表 ≤20 条 | | 显式 @ 的成员 id |
| `attachments` | 体 | `[{sha, name, mime}]` ≤10 条 | | 先 `POST /api/conversations/{conv_id}/attachments?name=` 上传（原始体）拿到 `sha` |

立即返回刚发的消息 `{id, role: "user", mentions, …}`，不等回答。

**3. 收回答** `GET /api/events`（长连接 SSE，查询 `since` 或请求头 `Last-Event-ID` 断点续接），或轮询 `GET /api/conversations/{conv_id}`。

| 旧参数 | 新参数 | 转换 |
|---|---|---|
| `service` | 路径 `project_id` | 原来服务 id 换成项目 id，业务系统里存一张对照 |
| `message` | `text` | 上限 20000 → 8000，更长的作为附件发 |
| `session_id`（不给 = 新会话） | 不给 → 先调第 1 步；给了 → 路径 `conv_id` | 会话 id 就是 `conv_id`，业务系统按用户存起来续接 |
| — | `mentions` | 新增：可以直接 @ 项目里的其他成员 |
| — | `attachments` | 新增：旧接口不能带文件 |

### `POST /svc/sessions/{session_id}/interrupt` → `POST /api/conversations/{conv_id}/seats/{agent_id}/stop`

| 参数 | 旧 | 新 |
|---|---|---|
| 会话 | 路径 `session_id` | 路径 `conv_id`（同一个值） |
| 打断谁 | 整个会话 | 路径 `agent_id`：正在回答的那个 Agent（会话视图的 `agent_id`，或被 @ 的成员） |
| 返回 | `{ok: true}` | `{ok: true}`；这一轮已经结束时 409 |
| 替谁调 | 令牌（`services.call`） | 那个 Agent 的所有者；公共 Agent 由这一轮的调用方（开会话的人）自己停 |

读者停不了主持人的 Agent：入口是主持人自己的 Agent 时，要让读者能中断，就由业务系统替主持人调这个接口，或改用公共 Agent 当默认 Agent。

### 网页上的服务：`GET /api/services`、`POST /api/services/{id}/sessions` → `GET /api/network`、`POST /api/projects/{id}/sessions`

| 旧 | 旧参数 | 新 | 新参数 |
|---|---|---|---|
| `GET /api/services` | 无 | `GET /api/network` 的 `projects` | 无 |
| `POST /api/services/{service_id}/sessions` | 体 `title` ≤80 | `POST /api/projects/{project_id}/sessions` | 体 `title` ≤120、`agent` ≤100 |
| 读、结束服务会话 | 普通会话接口 | `GET /api/conversations/{conv_id}`、`PATCH /api/sessions/{conv_id}` | 体 `title` ≤120、`archived`、`agent`（显式 `null` = 不要默认成员） |

## 六、`/svc` 的 SSE 事件 → `GET /api/events`

旧流只属于这一次请求、只有这一个会话；新流是这个用户的全部事件，要按 `conversation_id` 过滤。

| 旧事件 | 旧数据 | 新事件 | 新数据 / 怎么认 |
|---|---|---|---|
| `session`（第一条） | `{session_id}` | 没有 | 开会话接口直接返回 `id` |
| `delta` | `{text}`：入口 Agent 逐段的话 | 没有逐字流 | 回答在一轮结束时整条到（下一行） |
| `done` | `{text}`：这一轮的完整回答 | `message.added` | `conversation_id` 是这个会话、`message.role` 是 `agent` 的那条，`message.text`；之后 `conversation.updated` 的 `status` 回到 `idle` 表示这一轮完了 |
| `tool` | `{name, input: {brief}}` | 读者收不到 | `run.progress` 只推给 Agent 的所有者 |
| `progress` | `{kind: task \| tool \| text, …}`：委托与执行方的进展 | `conversation.updated` | 只给状态：`thinking`（在想）/ `waiting`（等委托）/ `idle`；逐步过程同上只给所有者 |
| `progress`（`kind: heartbeat`） | `{elapsed_s, working}` | 流本身的 `: keepalive` | 每 15 秒一条注释行 |
| `error` | `{message}` | `message.added` 的 `role: system` 那条 | `message.text` 是原因 |
| 15 分钟超时的 `error` | | 没有 | 异步了，没有这个上限 |

## 七、换完要多做的事

旧接口里一步做完、现在要业务系统自己补的：

1. **指定主持人**：每个项目由一个真实用户主持，建项目、配成员、邀请都替他调。
2. **设默认 Agent**：`POST /api/projects` 的 `default_agent`，否则项目会话里没 @ 的消息没人接。
3. **邀请读者**：替主持人 `POST …/invitations` `{email}`，再替读者 `POST …/invitation` `{accept: true}`；被邀请的人得先有账号（替他调一次任意接口）。
4. **存两张对照**：原服务 id → 项目 id；每个用户 → 他的 `conv_id`。
5. **改成异步收回答**：订阅 `GET /api/events` 或轮询，不再等一个请求返回。
6. **改确认方式**：模板、插件第二次调传 `confirm: true`。

逐步走一遍的例子见 [integration-project-sessions.md](integration-project-sessions.md) 场景一、场景二。
