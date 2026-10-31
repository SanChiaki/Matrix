# 项目插件

> 入口见 [AGENTS.md](../../AGENTS.md)。给插件作者的指南见 [docs/plugin-authoring.md](../plugin-authoring.md)。设计与取舍见 [docs/project-plugins.md](../project-plugins.md)，原型见
> [docs/prototypes/project-plugins.html](../prototypes/project-plugins.html)。
> 代码：`plugins.py`（清单、包、记录、cron、目录）、`plugin_sources.py`（安装策略、按地址取包、后台目录、上架申请）、`sandbox.py`、`engine.py`「项目插件」一节、`server.py` 的插件路由、
> `static/plugin-sdk.js`（插件页面里的桥客户端）、`static/app.js` 的插件标签页。示例：`examples/plugins/sales-pipeline/`。

插件让一个项目接上某种业务：**表**（项目档案的一部分）、**页面**（沙箱里的自定义代码）、**技能**、**触发**
（外部回调 / 定时 / 记录变化 → 往群聊发一条 @ 消息）。不硬编排：流程靠技能和 Agent 的判断推进，网络只负责把事情叫醒、
把数据放在档案里、管住权限（不变量 20）。

## 插件包

一个文件夹（上传时打成 zip 或选文件夹，可以多一层顶层目录；`__MACOSX`、`.DS_Store` 忽略，点开头或越界的路径拒绝）：

```
plugin.json            清单
page/                  页面代码（HTML / JS / CSS / 图片），只有这里的文件会被浏览器取到
skills/<名字>/SKILL.md  技能，和项目技能同一种格式
```

上限见 `models.PLUGIN_*`：包 ≤2 MB、≤300 个文件；每个项目 ≤10 个插件；每个插件 ≤5 个标签页、≤10 张表、≤20 个触发；
每张表 ≤40 个字段、≤5000 条记录，单条 ≤32 KB。

`plugin.json`（`plugins.parse_manifest` 校验，错误信息是中文、指出哪一项）：

| 键 | 说明 |
| --- | --- |
| `name` | 小写字母、数字、`-`，≤64；项目里唯一，也是数据目录名 |
| `version` | `x.y.z` |
| `title` / `description` | 给人看的名字与说明 |
| `tabs` | `[{key, label, entry}]`，`entry` 必须在 `page/` 下且在包里 |
| `tables` | `{key: {title, fields: [{key, label, type, options, required}]}}`；`type` 是 `text` `longtext` `number` `select` `date` `agent` `file` `bool` |
| `page_permissions` | 页面要用的能力，词表 `models.PLUGIN_PAGE_CAPS`：`records.read` `records.write` `archive.read` `members.read` `chat.read` `chat.post` `requests.read` |
| `agent_tools` | 开给 Agent 的记录工具：`records.read` / `records.write`（`models.PLUGIN_AGENT_CAPS`） |
| `triggers` | `[{key, type, table, cron, when, notify: {to, text}}]`，见“触发” |
| `connectors` | 用到的外部服务（连接器 id 或服务名，≤10）：只是声明，启用时列进项目的连接器面板（`engine.connector_panel`） |
| `release_views` | 能在交付件里打开的标签 key（要在 `tabs` 里）；那里页面是发布时的快照、桥只读快照（[releases.md](releases.md)） |

## 安装、升级、卸载

- 来源（[plugin-install.md](../plugin-install.md)）：部署方目录（`plugins.json`：`--plugins` / `MATRIX_PLUGINS`，默认 `<data>/plugins.json`，
  `{"plugins": ["相对目录", ...]}`，按文件与清单的修改时间热加载；加上管理后台加的 `<data>/plugin-catalog/`，`plugin_sources.CombinedCatalog`，
  同名以文件为准），主持人上传，或主持人填地址（`.zip` 插件包或 `plugin.json`，`install_plugin_url`，之后可“检查更新”）。**只有主持人**装、升级、卸载、启停触发（`hosted_project`）；
  共享项目的参与者看得到装了什么、申请了哪些能力（`project_detail.plugin_list`）。
- 装好的包在 `projects/<id>/plugins/<name>/`，元数据在 `Project.plugins[name]`
  （`version, title, source（catalog | upload | url）, url, runs（remote | sandbox | host | null）, sha256, by_user, installed_at, data_rev,
  triggers: {key: {enabled, by_user, failures, paused, last_fired, error, hook_hash}}`）。
  安装、升级、卸载、启停触发都是档案变化：`archive_rev` +1、写 `log.jsonl`、群聊里一条公开说明（写明“以看页面的人的名义”与申请的能力）。
- **确认**：表结构变窄（删表、删字段、改类型、收窄选项，`plugins.schema_changes`），或者是主持人自己装的带服务的插件（数据去向，
  `plugin_sources.disclosure`）时要 `confirm`，否则 `409 {detail, confirm: [一条条], digest}`（`engine.NeedsConfirm`）；确认时带回 `digest`，
  包的指纹对不上就再问一次。旧记录原样保留，不认识的字段在页面上不显示。已有的触发状态保留。
- **启停**：主持人在插件卡片上用开关停用 / 启用整个插件（`PUT /api/projects/<id>/plugins/<name> {enabled}`，`Project.plugins[name].disabled`）。
  停用后插件对外一律不可用（都经 `Network._manifest`）：标签页消失、帧令牌作废、桥与记录读写拒绝、Agent 的记录工具与提示、
  技能、档案推送里的插件一节都不带，外部回调 409、定时与记录触发不跑。包、记录、触发设置原样保留，升级不改变启停，重新启用即恢复。
  启停是档案变化（`archive_rev` +1、`log.jsonl`、群聊说明）。
- **卸载**：删包、触发与这个安装的签名密钥，记录留在 `projects/<id>/plugin-data/<name>/`，重新装回来还在（没有自动清理）。
- 触发默认不开，主持人逐个启用。

## 记录

- 存在 `projects/<id>/plugin-data/<plugin>/<table>.json`：`[{id, fields, created_at, created_by, updated_at, updated_by}]`；
  `created_by` / `updated_by` 是 `{"user"}`、`{"agent"}` 或 `{"trigger", "user"}`。
- 写入只经：项目页 / 插件页面的桥（以看的人）、外部回调（以启用触发的人）、Agent 的 `plugin_write`。按清单校验
  （必填、选项、数字、日期、`agent` 字段只能是项目成员——按名字或 id，存 id；`file` 字段只能是档案里的文件），修改只校验给出的字段。
- 每次变化追加到 `log.jsonl`（`plugin.record.create/update/delete`），`data_rev` +1，发 `plugin.records` 事件（按项目可见性过滤）；
  **不**增加 `archive_rev`、不在群聊里逐条播报——档案推送只带表结构，不带记录。
- 没有按记录的访问控制：项目里所有参与者看到同一份数据（同 Kylon，“筛选过的视图不是隐私边界”，research: kylon#apps）。

## 页面与桥

- 项目页每个插件标签页是一个 iframe：`sandbox="allow-scripts allow-forms"`（**不带** `allow-same-origin`，不透明来源；`allow-forms` 只为让表单的 `submit` 事件触发，提交被 CSP `form-action 'none'` 挡下）、
  `referrerpolicy="no-referrer"`，`src` 是 `POST /api/projects/<id>/plugins/<name>/frame` 发的 `/plugin-frame/<令牌>/page/<entry>`。
- 帧令牌：HMAC（进程内随机密钥），绑定项目、插件、版本、看的人，10 分钟有效（`engine.FRAME_TTL`）；页面里相对路径的资源随令牌取。
  `/plugin-frame/<令牌>/…` 只给这个插件 `page/` 下的文件和 `_matrix/sdk.js`，响应头带
  `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'none'; script-src 'self' 'unsafe-inline'; …; connect-src 'none'; form-action 'none'; frame-ancestors 'self'`、
  `nosniff`、`Referrer-Policy: no-referrer`、`no-store`。过期、改过、换版本、卸载、看的人看不到项目 → 404。
  直接打开这个地址同样在沙箱里（CSP 的 `sandbox`），拿不到 cookie。
- 桥：页面 `postMessage({matrix: 1, id, op, args})` → 父页面（`app.js` 只认当前插件 iframe 的 `source`）→
  `POST .../bridge {op, args}` → `Network.plugin_bridge`，回 `{matrix: 1, id, ok, result | error}`。
  能力 = 清单申请的 ∩ 看的人本身有的（`BRIDGE_OPS`）；没申请的调用拒绝并留痕（`plugin.violation`，`log.jsonl`）。
  `chat.read` 只给公开消息（没有 `audience` 的用户 / Agent / 事件消息），`requests.read` 按看的人的可见性。
  `ui.badge`（标签页上的小标记）、`ui.file`（打开档案文件预览）在父页面处理，不经服务器。
- **以人名义发言**：`chat.post` 只生成草稿（10 分钟、只属于这个人），父页面弹确认卡，人点“发送”才调
  `POST /api/plugin-drafts/<id>/send`；桥里没有发送这一步。发出的消息 `Message.via` 是插件名，群聊里标出“经 X 插件”。
- 父页面把 `plugin.records` 事件转给当前插件页面：`{matrix: 1, event: "records.changed", data: {table}}`；
  `context` 带主题（`theme`），页面可以跟着深浅色。

## Agent 这一侧

- **技能**：插件的 `skills/` 作为一层，排在项目技能之后、连接器自带与我的技能之前（重名时项目技能赢）。
- **档案推送**：`format_archive` 里“### 插件”一节列出每个插件的表与字段、能用的工具。
- **记录工具**：项目里有插件申请了 `agent_tools` 时，这个项目的席位与任务才挂上 `plugin_records`（查）/ `plugin_write`（建 / 改），
  按各插件自己申请的能力放行（`ExecContext.plugins`、`_agent_plugin`）。是档案类工具，不需要 `delegate`；
  结构化参数（`where`、`fields`）是 JSON 字符串（DeepSeek 下数组 / 对象参数容易写坏，见 AGENTS.md 工作方式第 5 条）。
- Agent 写的记录留痕为 Agent，但**不引发记录触发**：Agent 之间不能经插件互相唤醒（不变量 6）。
- **提议装插件**：档案类工具 `propose_plugin`（`name` 目录里的插件名或 `url` 插件地址，`reason`）。网络取包、按策略检查，生成
  `Proposal(kind="plugin")`（带标题、版本、来源、确认卡条目、包的指纹），由主持人在群聊卡片上批准（`install_proposed_plugin`，
  以主持人的身份装提议时那一份）或拒绝，结果作为通知送回提议的 Agent（`format_proposal_notice`）。算 `OUTBOUND_TOOLS`。

## 触发

每个触发只做一件事：在项目群聊里发一条 `@<notify.to> <notify.text>`（`{字段}` 换成记录或回调里的值），
发消息的人是**启用它的人**（`by_user`），`via` 是插件名。被 @ 的 Agent 照常接任务；它据此登记的用户任务，下发人也是这个人。
触发不派委托、不改状态、不串步骤。

| `type` | 什么时候 | 说明 |
| --- | --- | --- |
| `webhook` | 外部系统 `POST /hooks/<令牌>` | 主持人点“生成地址”拿到一次明文令牌，网络只存 sha256；重置后旧地址立即失效。不要登录。有 `table` 时请求体按字段映射成一条记录（清单以外的键丢弃）。每个令牌每分钟 ≤60 次（`engine.HOOK_RATE`，超了 429）；停用 409，未知 404，不是 JSON 400 |
| `schedule` | cron（5 段，本地时间） | 分钟位最多 6 个值（间隔至少 10 分钟）；后台线程每分钟检查一次，每个触发每分钟最多一次 |
| `record` | 人写了记录 | `when: {event: "create"}` 或 `{field, equals}`（变成这个值的那一刻）；Agent 写的不算 |

目标成员不在项目里、启用的人已离开项目等失败计数，连续 3 次暂停（群聊里说明），主持人重新启用后清零。

## 插件服务与挂点

设计见 [plugin-hooks.md](../plugin-hooks.md)，不变量 24；示例 `examples/plugins/llm-wiki/`、演示 `examples/llm-wiki/`。

- **清单**：`service`：`command`（网络托管：`python` 是网络自己的解释器（有 `requirements` 时是按锁文件建的环境的），或部署机器上的程序名；
  `args` 里 `{plugin}` 换成装进项目的包目录、`{data}` 换成数据目录）或 `url`（远程 MCP；http 只在部署方白名单里放行），二选一；
  `tools`：工具名 → `read` / `write` / `hook`，只有这里列的开给 Agent 与页面，`<插件名>__<工具名>` 最长 64；`network`：托管服务要不要联网
  （沙箱据此开网络，只有目录里的能是 true）；`requirements`：包里的依赖锁文件（每个包带 `--hash`）。
  `hooks`：`mount`（`resources`：一个 MCP 资源 URI 前缀 → 工作目录 `plugins/<插件>/<as>/`，去掉前缀是相对路径）、`context`（`resource` 或 `tool`
  二选一，`label`、`budget` ≤4000；`tool` 只能是 read 类，`args` 模板可用 `{latest}`、`{mission}`）、`after`（`on` 第一期只有 `request.accepted`；
  `notify.to` 写成员名或 `{agent}` ＝接任务的 Agent，`text` 可用 `{title}`、`{summary}`、`{agent}`；或 `tool` + `args`）。校验在 `plugins._service` / `_hooks`。
- **安装**（[plugin-install.md](../plugin-install.md)）：`_plugin_runs` 定在哪里跑：目录里的远程服务 `remote`、托管的有沙箱且
  `sandbox_catalog` 时 `sandbox` 否则 `host`；主持人自己装的远程服务要过 `plugin_sources.check_remote`（部署方策略），托管的只能 `sandbox`
  （要有沙箱、`hosted_upload`、不联网、`command` 是 python）。运行时 `_service_client` 按安装来源再判一次（策略改了立即生效）。
  有 `requirements` 时安装就建依赖环境（`plugin_service.build_env`）。升级、停用、卸载都停掉进程；托管进程的数据在
  `projects/<id>/plugin-data/<插件>/service/`，卸载不删；远程服务的数据在服务那边。
- **沙箱**（`sandbox.py`）：Linux bubblewrap，新命名空间（默认不共享网络），只读挂系统目录、解释器与依赖环境、插件包，读写挂数据目录，
  空的 `/tmp`；`setrlimit` 限地址空间、打开文件、文件大小、CPU 时间。`available()` 第一次真跑一次探测；`MATRIX_PLUGIN_SANDBOX=off` 关掉。
- **托管进程**（`plugin_service.py`）：`ServiceHost` 按 (项目, 插件) 一个 `StdioClient`，第一次用到时启动（`initialize` + `tools/list`），
  版本换了重启；一次调用最多等 60 秒，串行；空闲 15 分钟由定时线程停掉；连续 3 次起不来就暂停，主持人停用再启用清零。
  环境只有 `PATH`、`SYSTEMROOT` 等运行必需的变量，`HOME` 指向数据目录；stderr 只留最后 20 行给部署方排错。服务退出随 Matrix 一起停。
- **远程服务**：`RemoteService` 复用 `mcp_client.RemoteMCP`；每次调用带 `Authorization: Bearer v1.<项目>.<过期>.<签名>`
  （`sign_token`：HMAC-SHA256 签 `v1.<项目>.<插件>.<过期>`，10 分钟）。目录里的：密钥在环境变量 `MATRIX_PLUGIN_SECRET_<插件名>`（大写，`-` 换 `_`），
  部署方在服务那边配同一个值，没配时这个插件的工具不出现、挂点为空、调用报“部署方没配签名密钥”。主持人自己装的：每个安装一把
  （`ps_…`，保管库 `plugin-<项目>-<插件>`），安装或重置时只返回一次（`reset_plugin_secret`），卸载即删；第三方那里人调用时 `_meta.matrix/caller`
  只带成员 id。
- **工具**：按原名 `<插件>__<工具>` 出现，带服务自己的 schema 与说明（说明前加“[插件「标题」只读 / 写入]”）。网络工具的 MCP 服务
  （`mcp_server.py`）在 `tools/list` 时用保留名 `__list_tools__`（`models.LIST_TOOLS`）经同一条回调取（`Network.plugin_tool_specs`），
  调用同样走 `/internal/tools`，`handle_tool` 认出 `<插件>__` 前缀后交给 `_plugin_tool`。只在装了它的项目里的群聊席位、项目会话、委托里有
  （`ExecContext.services`）。写类要 `file.write`；会话读过越界来源时按不变量 22 拒绝。每次调用写一行 `plugin.service.call` 进项目 `log.jsonl`
  （工具、读写、成败、谁），不记参数与结果；写类成功后插件 `data_rev` 加一、推 `plugin.records`，页面据此刷新。
- **挂点在一轮里**（`Network._run`）：都在锁外向服务取。`context` 在工作台账之后加一段“项目插件提供的上下文”，资源类按指纹（`hooks_seen`）
  没变不重推，工具类每轮调；全部插件合计 ≤8000 字符。`mount` 先 `resources/list`、再逐个 `resources/read`（最多 2000 个、单个 ≤2 MB），
  结果放在 `ExecContext.plugin_files`：云侧写进工作目录、运行后丢弃，端侧随 `run.request` 的材料下发。`after` 在 `review_request` 与
  `accept_request` 验收之后调用（`_after_accepted`），只接项目群聊里的任务；`notify` 以验收的人的身份、`via` 插件标题发进群聊。
  失败写 `plugin.after.failed` 进日志。
- **页面**：桥的 `service.call` 要申请 `service.read`，只能调 read 类工具；SDK 是 `matrix.service.call(tool, args)`。发布物里的页面没有服务。
- **工具分级 `hook`**：只给挂点（`context`、`route`）调，不出现在 Agent 的工具里，页面也调不到。
- **route 挂点**（设计见 [plugin-hooks.md](../plugin-hooks.md) 第 8 节）：`hooks.route` 最多一个，`on` 只有 `message.posted`，`tool` 是 read 或 hook 类，
  `may` 取 `wake` / `claim`。项目群聊里人发的消息（`send_message`）和 Agent 的回复（`_seat_round` 末尾，被拦下等确认的不算）落进群聊后，
  后台调 `_route`（登记进 `_running`，`wait_quiet` 会等它）。人发的消息开一串接力（`_cascades`：预算 `ROUTE_BUDGET`、链上的成员），
  @ 到的成员与 route 唤醒的成员在收件箱项里带上接力 id，席位记在 `_seat_cascade`，它的回复沿用。闸门依次检查：申请了这个种类、
  是项目成员、接力还在、不在链上、认领只对人发的没 @ 人且没被认领的消息、席位没卡住、预算还有。接受的：预算减一、进链、认领的写
  `Message.claimed_by`，往席位收件箱放 `{"kind": "route", …}` 并唤醒，群聊里记一条事件；认领那一轮 `ctx.addressed` 带上这条消息，
  `open_request` 的下发人是原作者。每次写一行 `plugin.route`（接受、拒绝与理由）进项目 `log.jsonl`；服务出错写 `plugin.route.failed`。
  预算用完时群聊里说一次。接力只在内存里，重启后不再自动唤醒。
  传给服务的还有 `relay`（剩余预算、链上的成员）与 `members[].assistant`。只因 route 的 `wake` 醒来的一轮（没有认领、没被 @），
  回复正好是 `ROUTE_PASS`（“（略过）”）时不发进群聊（`_seat_round`）。
- **调用者身份**：`_service_call` 给每次工具调用带 `_meta`：`matrix/caller`（Agent 调的是 `{"kind": "agent", "id", "name"}`，页面是
  `{"kind": "user", "email"}`（自己装的远程服务是 `{"kind": "user", "id"}`），挂点是 `{"kind": "network"}`）与 `matrix/project`。挂点（hook 类）调用成功后也算改了数据（`data_rev` 加一、
  推 `plugin.records`）。

## 接口

| 方法 | 路径 | 谁 |
| --- | --- | --- |
| GET | `/api/plugins` | 登录用户：部署方目录 |
| POST | `/api/projects/<id>/plugins` `{name, confirm, digest}` | 主持人：从目录安装 / 升级 |
| POST | `/api/projects/<id>/plugins/upload?confirm=1&digest=` | 主持人：zip 请求体，或 JSON `{files: [{path, data(base64)}]}` |
| POST | `/api/projects/<id>/plugins/url` `{url, confirm, digest}` | 主持人：按地址安装 |
| POST | `/api/projects/<id>/plugins/<name>/refresh` `{confirm, digest}` | 主持人：按地址装的检查更新 |
| POST | `/api/projects/<id>/plugins/<name>/secret` | 主持人：生成 / 重置自己装的远程插件的签名密钥（明文只这一次） |
| POST | `/api/plugin-submissions?note=` | 登录用户：提交上架申请（请求体同上传） |
| GET | `/api/plugin-submissions` | 登录用户：自己的申请 |
| POST | `/api/proposals/<id>/install` `{note}` | 主持人：批准 Agent 装插件的提议 |
| GET / PUT | `/api/admin/plugins`、`/api/admin/plugins/policy` | 管理员：插件页、安装策略 |
| POST / DELETE | `/api/admin/plugins/catalog/git`、`…/upload`、`…/<name>/refresh`、`…/<name>` | 管理员：后台目录 |
| GET / POST | `/api/admin/plugins/submissions/<id>/file?path=`、`/api/admin/plugins/submissions/<id>` `{approve, note}` | 管理员：审核上架申请 |
| PUT | `/api/projects/<id>/plugins/<name>` `{enabled}` | 主持人：启用 / 停用整个插件 |
| DELETE | `/api/projects/<id>/plugins/<name>` | 主持人：卸载 |
| PUT | `/api/projects/<id>/plugins/<name>/triggers/<key>` `{enabled}` | 主持人 |
| POST | `/api/projects/<id>/plugins/<name>/triggers/<key>/hook` | 主持人：生成 / 重置回调地址（明文只这一次） |
| GET | `/api/projects/<id>/plugins/<name>/records/<table>` | 项目参与者 |
| POST | `/api/projects/<id>/plugins/<name>/frame` `{tab}` | 项目参与者：发帧令牌 |
| POST | `/api/projects/<id>/plugins/<name>/bridge` `{op, args}` | 项目参与者（父页面代插件页面调） |
| POST | `/api/plugin-drafts/<id>/send` | 草稿的主人（确认卡） |
| GET | `/plugin-frame/<令牌>/<path>` | 凭帧令牌 |
| POST | `/hooks/<令牌>` | 凭回调令牌 |

## 还没做

- 插件服务：`hooks.settings`（安装时让主持人填的设置）、更多 `after` 事件；macOS / Windows 的沙箱、cgroup 级上限、
  托管插件按域名出网（见 plugin-install.md 第 9 节）。
- 回调请求的 HMAC 签名校验（目前令牌即授权）；插件之间互读表。
