# 业务系统对接 Matrix（对接指南）

> 给业务系统（报销、CRM、工单……）的开发者看：怎么拿到令牌、怎么调接口、要注意什么。规则与取舍见设计文档
> [open-api.md](open-api.md)（不变量 30）；可以直接跑的示例客户端在 `examples/open-api/client.py`（只用 Python 标准库）。
> 改了 `matrix/apps.py`、`matrix/open_server.py` 的接口，要同步改这份指南与示例。

## 一、能做什么

| 能力 | 说明 | 授权范围（scope） |
| --- | --- | --- |
| 查 | 列出能用的 Agent 与模板 | `agents.read` |
| 组 | 建项目（直接建，或用模板建）；拉成员；写纲要、角色；上传资料、设可见范围；加项目技能；从部署方目录装插件；删它自己建的项目 | `projects.write` |
| 发 | 把项目作为服务发布、修改、撤回；看服务用量 | `services.publish` |
| 调 | 调用服务（自己发布的，或别人开放给你的） | `services.call` |

做不到的：读项目里的群聊、会话、委托过程，读任何人的私有目录和凭证，新建 Agent。应用自己调用服务的那些会话除外，那是它自己的。

## 二、接入前准备

### 1. 选一种身份

| | 业务系统账号（推荐给长期运行的系统） | 代表一个人 |
| --- | --- | --- |
| 令牌代表谁 | 这个系统自己（`<应用 id>@apps.matrix`） | 签令牌的那个人 |
| 谁签令牌 | Matrix 管理员：管理后台 → 外部应用 → 签令牌 | 用户自己：更多 → 外部应用 → 授权一个应用代表你 |
| 建的项目归谁 | 系统账号当主持人，负责人（真人）在网页上等同主持人 | 那个人当主持人 |
| 能管哪些项目 | 它主持的全部 | 经这个应用建的，加上那个人授权时另外交给它的 |
| 调用服务时调用方是谁 | 系统账号；要用业务系统时用它自己的连接器授权（负责人或管理员代配） | 那个人；用他自己的授权 |

### 2. 找管理员登记

把这些交给 Matrix 管理员：

- 应用名称与说明（会出现在群聊事件里：“……（经「报销系统」）”）；
- 要哪些授权范围（见第一节的表）；
- 要不要业务系统账号，要的话至少一名**负责人**（Matrix 用户的邮箱）。负责人能在网页上看、管系统账号主持的项目，能代它授权连接器。

管理员登记后签发令牌（`mat_…`），**只显示一次**。把它存进业务系统的密钥管理里，不要写进代码仓库、日志或前端。丢了就让管理员重签，旧的立刻作废。

### 3. 网络

- 业务系统能访问到 Matrix 的地址（下文写作 `$MATRIX`）。
- 部署用的 Caddy 已经放过 `/open/v1/*` 与 `/svc/*`，这两类路径不要 basic auth，只凭令牌（`deploy/Caddyfile`）。其余路径照旧要登录，业务系统用不到。

## 三、通用约定

- **鉴权**：每个请求带 `Authorization: Bearer mat_…`。不走 Cookie 登录。
- **格式**：请求与返回都是 JSON（上传资料除外，见第五节）；UTF-8。
- **路径里的中文要 URL 编码**（比如资料路径 `公开/手册.md`）。
- **id**：Agent、项目、服务、会话都用 Matrix 返回的 id（`agent-…`、`prj-…`、`svc-…`、`conv-…`）。
- **限频**：每把令牌每分钟最多 600 次。

### 错误

| 状态码 | 什么时候 | 返回 |
| --- | --- | --- |
| 401 | 令牌认不出、已撤销、应用已停用 | `{"detail": "令牌无效、已撤销或应用已停用"}` |
| 403 | 令牌没有这个接口要的授权范围 | `{"detail": "这把令牌没有授权 …"}` |
| 404 | 东西不存在，或者你没权限碰它（一律按不存在处理，不泄露） | `{"detail": "unknown project prj-…"}` |
| 409 | 业务上不行（比如名字重复、角色没指给成员） | `{"detail": "……"}`，中文说明原因 |
| 409（要确认） | 用模板、装插件之前要先看要确认的事 | `{"detail": "…", "confirm": ["…", "…"], "digest": "…"}` |
| 422 | 请求体字段不对 | FastAPI 的校验说明 |
| 429 | 调用太频繁 | `{"detail": "调用太频繁，稍后再试"}` |

**要确认的操作**：用模板建项目、装插件，第一次调用会返回 409 和 `confirm`（一条条要确认的事）、`digest`。业务系统看过（或记录下来）之后，
**原样再发一次，加上 `"digest": "…"`**，才真正执行。模板或插件在这期间换了版本，会再返回一次 409。

## 四、快速开始（curl）

```bash
export MATRIX=https://matrix.corp.example
export TOKEN=mat_xxxxxxxx
```

1. 查能用的 Agent：

```bash
curl -s -H "Authorization: Bearer $TOKEN" $MATRIX/open/v1/agents
```

```json
[
  {"id": "agent-3198a2e8", "name": "财务助手", "description": "查报销进度、解释报销规则", "capabilities": ["报销"],
   "authority": ["web.research", "delegate"], "kind": "public", "owner": "ops@x.io", "connectors": [],
   "where": "cloud", "platform": null, "online": true},
  {"id": "agent-a4e33d04", "name": "合同审核", "description": "审核合同条款", "capabilities": ["合同"],
   "authority": ["web.research"], "kind": "opened", "owner": "ann@x.io", "connectors": [],
   "where": "cloud", "platform": null, "online": true}
]
```

`kind`：`public` 是公共 Agent；`mine` 是令牌代表的人自己的；`opened` 是所有者开放给这个应用的个人 Agent。

2. 用它们建项目：

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' $MATRIX/open/v1/projects \
  -d '{"name": "报销流程", "brief": "线上报销：5000 以下直接批", "members": ["agent-3198a2e8", "agent-a4e33d04"]}'
```

3. 上传给服务读的资料（请求体就是文件内容）：

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" --data-binary @手册.md \
  "$MATRIX/open/v1/projects/prj-759811d1/files/%E5%85%AC%E5%BC%80/%E6%89%8B%E5%86%8C.md"
```

4. 发布成服务：

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $MATRIX/open/v1/projects/prj-759811d1/services -d '{
    "name": "intake", "title": "报销服务", "description": "报销咨询与提交",
    "entry": "agent-3198a2e8", "staff": ["agent-a4e33d04"], "authority": ["delegate", "web.research"],
    "scope": ["公开"], "inbox": "收件", "public": true}'
```

5. 调用服务（流式返回）：

```bash
curl -sN -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $MATRIX/svc/agents/svc-79c7d01a/chat -d '{"message": "我要报销 300 元"}'
```

```
event: session
data: {"session_id": "conv-88e1c57e"}

event: tool
data: {"name": "mcp__matrix_net__delegate", "input": {"brief": "合同审核"}}

event: done
data: {"text": "……最终回复……"}
```

6. 看用量：

```bash
curl -s -H "Authorization: Bearer $TOKEN" $MATRIX/open/v1/projects/prj-759811d1/services/svc-79c7d01a/usage
```

```json
{"days": 30, "sessions": 1, "runs": 1, "failed": 0, "tokens": {"input": 0, "output": 0, "cached": 0, "total": 0},
 "users": [{"user": "app-75112474@apps.matrix", "runs": 1, "failed": 0, "tools": 0, "tokens": 0, "last_at": 1795055450.06}]}
```

同样的流程用示例客户端：

```bash
MATRIX_URL=$MATRIX MATRIX_TOKEN=$TOKEN python examples/open-api/client.py agents
```

```bash
MATRIX_URL=$MATRIX MATRIX_TOKEN=$TOKEN python examples/open-api/client.py build 报销流程 agent-3198a2e8 agent-a4e33d04
```

```bash
MATRIX_URL=$MATRIX MATRIX_TOKEN=$TOKEN python examples/open-api/client.py chat svc-79c7d01a "我要报销 300 元"
```

不要了就删掉（只能删经这个应用建的项目）：

```bash
MATRIX_URL=$MATRIX MATRIX_TOKEN=$TOKEN python examples/open-api/client.py delete prj-759811d1
```

## 五、接口参考

### 查（`agents.read`）

| 接口 | 说明 |
| --- | --- |
| `GET /open/v1/agents` | 能用的 Agent，字段见第四节第 1 步 |
| `GET /open/v1/blueprints` | 能用的模板：`id`、`title`、`description`、`roles`（角色名）、`plugins`、`params`（参数个数）等 |

### 组（`projects.write`；只读的两个 `GET` 有 `services.publish` 也行）

| 接口 | 请求 | 说明 |
| --- | --- | --- |
| `GET /open/v1/projects` | — | 能管的项目列表 |
| `GET /open/v1/projects/{id}` | — | 项目配置：`name`、`description`、`brief`、`members`、`participants`、`roles`、`files`（路径）、`skills`、`plugins`、`services`。**没有群聊与会话内容** |
| `POST /open/v1/projects` | `{name, description?, brief?, members?: [agent id], blueprint?, params?, roles?: {角色 key: 成员 agent id}, digest?}` | 建项目，返回项目配置 |
| `DELETE /open/v1/projects/{id}` | — | 删项目：**只能删经这个应用建的**（授权人另外交给它管的已有项目只能管、不能删，返回 409）。有进行中的对话时不能删（409）；发布的服务一并撤回，服务会话一起删，群聊归档为只读 |
| `PATCH /open/v1/projects/{id}` | `{description?, brief?, roles?}` | `roles` 的格式同网页上的项目角色：`{key: {name, description, members: [agent id]}}` |
| `PUT /open/v1/projects/{id}/members/{agent}` | — | 拉一个成员进来 |
| `DELETE /open/v1/projects/{id}/members/{agent}` | — | 移出 |
| `PUT /open/v1/projects/{id}/files/{path}` | 请求体是文件内容，任何类型（≤100 MB，与网页上传同一个上限） | 上传资料，同名覆盖；`path` 可以带目录 |
| `PATCH /open/v1/projects/{id}/access` | `{path, rule: {roles?: [角色 key], agents?: [agent id]} \| null}` | 资料（文件或目录）的可见范围；`null` 是所有成员可见 |
| `POST /open/v1/projects/{id}/skills` | `{name, description, body}` | 写一个项目技能（`name` 用小写字母、数字、`-`） |
| `POST /open/v1/projects/{id}/plugins` | `{name, digest?}` | 从部署方的插件目录装（要确认，见第三节） |

几条要知道的：

- **成员只能是查得到的 Agent**。拉进开放给你的个人 Agent（`kind: opened`）时，它的所有者会自动成为这个项目的参与者，看得到群聊与档案。
  这和他受邀加入后带进自己的 Agent 一样，审批、提问照常找他。
- **用模板**：`blueprint` 带上模板 id，`roles` 必须把模板里的**每个角色**都指给 `members` 里的成员，因为应用不能新建 Agent。
  第一次返回 409 要确认（第三节），带 `digest` 再发一次。
- **业务系统账号建的项目**，负责人会自动成为参与者，但他们的助理不会跟着进来：项目成员只有你在 `members` 里点名的那些。
  负责人在网页上开项目会话时，Matrix 会先问他要不要把自己的助理加进来，同意了成员里才会多出它。
- 每次写操作都会在项目群聊里留一条事件，后缀“（经「应用名」）”，档案日志里记 `via_app`。

### 发（`services.publish`）

| 接口 | 请求 | 说明 |
| --- | --- | --- |
| `POST /open/v1/projects/{id}/services` | 见下表 | 发布即生效，返回服务名片与配置 |
| `PATCH /open/v1/projects/{id}/services/{sid}` | 同下表的任意几项 | 改了就切新版本，下一轮起生效 |
| `DELETE /open/v1/projects/{id}/services/{sid}` | — | 撤回：已有的服务会话变成只读 |
| `GET /open/v1/projects/{id}/services/{sid}/usage?days=30` | — | 用量：会话数、轮数、失败、token、按调用方汇总；**不给内容** |

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 小写字母、数字、`-`，项目内唯一（PATCH 不能改） |
| `title`、`description` | `title` 是 | 名片上的标题与“能办什么” |
| `entry` | 是 | 入口：项目里的一个 Agent，调用方只和它对话 |
| `staff` | 否 | 入口能委托的 Agent。**不写就是项目里全部 Agent**，包括负责人、参与者的助理，建议显式写 |
| `authority` | 否 | 服务授予的权限（如 `delegate`、`web.research`、`file.write`、`connector.use`）。**不写就是没有任何权限**：入口不能委托、不能在工作目录里写文件（也就没有东西能放进收件目录）、不能用连接器 |
| `scope` | 否 | 能读的资料（文件或目录，实时内容）；不写就不读资料 |
| `inbox` | 否 | 收件目录：服务里唯一能写的地方（`publish_file`），不通知；不写就不能写 |
| `plugins` | 否 | 服务里能用的项目插件，照插件原来的方式读写 |
| `readers` | 否 | 给谁用（Matrix 用户的邮箱，也可以是别的业务系统账号 `app-…@apps.matrix`） |
| `public` | 否 | `true`：所有登录用户都能用 |

服务产生的模型用量**记在项目下**，目前不设上限。

### 调（`services.call`）

见第六节。

## 六、调用服务（标准 HTTP 形状）

| 接口 | 说明 |
| --- | --- |
| `GET /svc/agents/{service}` | 名片：`{"name", "description"}`。只认令牌代表的主体看得到、在发布中的服务 |
| `POST /svc/agents/{service}/chat` | 请求 `{"message", "session_id"?}`，返回 `text/event-stream` |
| `POST /svc/sessions/{session_id}/interrupt` | 中断这一轮；没在跑时也返回 `{"ok": true}` |

### 事件

| 事件 | 数据 | 说明 |
| --- | --- | --- |
| `session` | `{"session_id"}` | **总是第一个**。续接时带回来；它只属于你，别人（包括项目的主持人）看不到 |
| `delta` | `{"text"}` | 入口这一轮说的话；入口把活委托出去还在等时，中途的回复也以 `delta` 送出 |
| `tool` | `{"name", "input": {"brief"}}` | 入口或岗位在调工具：只有工具名与一行摘要 |
| `done` | `{"text"}` | 这一串做完了：最终回复。流到这里结束 |
| `error` | `{"message"}` | 这一轮失败了，或者等了 15 分钟还没结束（带着 `session_id` 再问一次可以接着拿） |

- **续接**：带上 `session_id` 就是同一个服务会话，入口记得之前说过什么。会话不设期限。
- **一次请求等到这一串做完**：入口委托岗位时，流会一直开着，直到最终回复（最多 15 分钟）。HTTP 客户端的读超时要设得比这长。
- **业务授权**：服务里要用业务系统（连接器）时，用的是**调用方自己的授权**。用业务系统账号调用时，就是负责人或管理员给系统账号配的连接器，从不借用项目里任何个人的授权。
- **另一个 Matrix 也能这样接**：事件格式和外部平台的标准形状一字不差，在那边的平台目录里登记 `"driver": "standard"`、`"url": "$MATRIX/svc"`，
  API Key 填应用令牌，服务 id 当作平台上的 Agent id（见 [remote-agent-authoring.md](remote-agent-authoring.md)）。

## 七、安全与留痕

- 令牌只存在业务系统的服务端；前端、移动端都不要拿到它。
- 令牌权限按最小授予：只调服务的系统，只要 `services.call`。
- 管理员停用应用、用户撤销授权，令牌**立刻**失效（401）。业务系统要能处理 401：告警，并找管理员重签。
- 应用做的每件事都留痕：群聊事件“（经「应用名」）”、档案日志 `via_app`、管理后台的调用次数与最近使用时间。
- Matrix 返回的内容里可能有项目资料、Agent 回复：按你们公司的数据分级处理，不要原样转发到外网。

## 八、对接检查清单

- [ ] 管理员登记了应用，授权范围只给需要的；要业务系统账号的，指定了负责人
- [ ] 令牌放进了密钥管理，没有出现在代码、日志、前端里
- [ ] `GET /open/v1/agents` 返回 200，查到的 Agent 符合预期
- [ ] 建的项目在 Matrix 网页上能看到，主持人与负责人显示正确，群聊里有“（经「应用名」）”的事件
- [ ] 发布服务时写了 `entry`、`staff`、`authority`、`scope`；需要写入时配了 `inbox` 并给了 `file.write`
- [ ] `/svc/…/chat` 能拿到 `session`→`done`；续接能接上；中断能停下；读超时大于 15 分钟
- [ ] 要用业务系统时，系统账号的连接器已经由负责人或管理员配好
- [ ] 撤销令牌后，业务系统收到 401 并能告警
- [ ] （上线前）Caddy 确认 `/open/v1/*`、`/svc/*` 不走 basic auth、其余路径照旧要
