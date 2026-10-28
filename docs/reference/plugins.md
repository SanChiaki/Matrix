# 项目插件

> 入口见 [AGENTS.md](../../AGENTS.md)。给插件作者的指南见 [docs/plugin-authoring.md](../plugin-authoring.md)。设计与取舍见 [docs/project-plugins.md](../project-plugins.md)，原型见
> [docs/prototypes/project-plugins.html](../prototypes/project-plugins.html)。
> 代码：`plugins.py`（清单、包、记录、cron、目录）、`engine.py`「项目插件」一节、`server.py` 的插件路由、
> `static/plugin-sdk.js`（插件页面里的桥客户端）、`static/app.js` 的插件标签页。示例：`examples/plugins/sales-pipeline/`。

插件让一个项目接上某种业务：**表**（项目档案的一部分）、**页面**（沙箱里的自定义代码）、**打法**（技能）、**触发**
（外部回调 / 定时 / 记录变化 → 往群聊发一条 @ 消息）。不硬编排：流程靠打法和 Agent 的判断推进，网络只负责把事情叫醒、
把数据放在档案里、管住权限（不变量 20）。

## 插件包

一个文件夹（上传时打成 zip 或选文件夹，可以多一层顶层目录；`__MACOSX`、`.DS_Store` 忽略，点开头或越界的路径拒绝）：

```
plugin.json            清单
page/                  页面代码（HTML / JS / CSS / 图片），只有这里的文件会被浏览器取到
skills/<名字>/SKILL.md  打法，和项目技能同一种格式
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
| `release_views` | 能在事实发布物里打开的标签 key（要在 `tabs` 里）；那里页面是发布时的快照、桥只读快照（[releases.md](releases.md)） |

## 安装、升级、卸载

- 来源：部署方目录 `plugins.json`（`--plugins` / `MATRIX_PLUGINS`，默认 `<data>/plugins.json`，`{"plugins": ["相对目录", ...]}`，
  按文件与清单的修改时间热加载），或主持人上传。**只有主持人**装、升级、卸载、启停触发（`hosted_project`）；
  共享项目的参与者看得到装了什么、申请了哪些能力（`project_detail.plugin_list`）。
- 装好的包在 `projects/<id>/plugins/<name>/`，元数据在 `Project.plugins[name]`
  （`version, title, source, by_user, installed_at, data_rev, triggers: {key: {enabled, by_user, failures, paused, last_fired, error, hook_hash}}`）。
  安装、升级、卸载、启停触发都是档案变化：`archive_rev` +1、写 `log.jsonl`、群聊里一条公开说明（写明“以看页面的人的名义”与申请的能力）。
- **升级**：表结构变窄（删表、删字段、改类型、收窄选项）时要 `confirm`，否则返回将要发生的变化（`plugins.schema_changes`）；
  旧记录原样保留，不认识的字段在页面上不显示。已有的触发状态保留。
- **启停**：主持人在插件卡片上用开关停用 / 启用整个插件（`PUT /api/projects/<id>/plugins/<name> {enabled}`，`Project.plugins[name].disabled`）。
  停用后插件对外一律不可用（都经 `Network._manifest`）：标签页消失、帧令牌作废、桥与记录读写拒绝、Agent 的记录工具与提示、
  打法、档案推送里的插件一节都不带，外部回调 409、定时与记录触发不跑。包、记录、触发设置原样保留，升级不改变启停，重新启用即恢复。
  启停是档案变化（`archive_rev` +1、`log.jsonl`、群聊说明）。
- **卸载**：删包与触发，记录留在 `projects/<id>/plugin-data/<name>/`，重新装回来还在（没有自动清理）。
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

- **打法**：插件的 `skills/` 作为一层，排在项目技能之后、连接器自带与我的技能之前（重名时项目技能赢）。
- **档案推送**：`format_archive` 里“### 插件”一节列出每个插件的表与字段、能用的工具。
- **记录工具**：项目里有插件申请了 `agent_tools` 时，这个项目的席位与任务才挂上 `plugin_records`（查）/ `plugin_write`（建 / 改），
  按各插件自己申请的能力放行（`ExecContext.plugins`、`_agent_plugin`）。是档案类工具，不需要 `delegate`；
  结构化参数（`where`、`fields`）是 JSON 字符串（DeepSeek 下数组 / 对象参数容易写坏，见 AGENTS.md 工作方式第 5 条）。
- Agent 写的记录留痕为 Agent，但**不引发记录触发**：Agent 之间不能经插件互相唤醒（不变量 6）。

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

## 接口

| 方法 | 路径 | 谁 |
| --- | --- | --- |
| GET | `/api/plugins` | 登录用户：部署方目录 |
| POST | `/api/projects/<id>/plugins` `{name, confirm}` | 主持人：从目录安装 / 升级 |
| POST | `/api/projects/<id>/plugins/upload?confirm=1` | 主持人：zip 请求体，或 JSON `{files: [{path, data(base64)}]}` |
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

- 回调请求的 HMAC 签名校验（目前令牌即授权）；`propose_plugin`（Agent 提议插件、主持人批准）；插件之间互读表。
