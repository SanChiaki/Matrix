# 连接器与技能

> 入口见 [AGENTS.md](../../AGENTS.md)。设计与取舍见 [docs/connectors-and-skills.md](../connectors-and-skills.md)。
> 代码：`engine.py`「连接器与技能」一节、`skills.py`、`connector_catalog.py`、`vault.py`、`mcp_client.py`、`mcp_connectors.py`。

两个并列的用户概念：**连接器**是外部服务的接入，要授权，动的是授权人的状态，默认隔离；**技能**是一种做法
（`SKILL.md` + 可选文件），不要授权，是能力，可以共享。个人层管“我有什么”，项目层管“这里用到了什么、这里怎么做”
（项目页上的“技能”标签是项目技能，“连接器”标签是下面的连接器面板）。

## 技能

| 层 | 在哪 | 谁写 | 谁能看 |
| --- | --- | --- | --- |
| 项目技能 | `projects/<id>/skills/<名字>/`，元数据在 `Project.skills` | 主持人（UI）或经主持人批准的 `propose_skill` 提议 | 项目里所有人（档案的一部分，不变量 17） |
| 连接器自带的技能 | `cache/connector-skills/<连接器>/`，从 `connectors.json` 生成 | 部署方 | 用到这个连接器的那一轮 |
| 我的技能 | `users/<uid>/skills/<名字>/`；Agent 勾选在 `Agent.skills` | 本人 | 只有本人（不变量 13）；勾上的跟着 Agent 进共享项目 |
| Agent 自己攒的 | 私有目录里的 `agent-home/skills/` | Agent 自己 | 网络不读（不变量 8） |

- **维护方式**：技能按 Agent Skills 标准在本地写好（一个文件夹：`SKILL.md` 的 frontmatter 有 `name`、`description`，
  可带 `scripts/`、`references/`、`assets/` 等，二进制也行），打包成 zip 或选文件夹上传（`POST .../skills/upload`，
  zip 请求体，或 JSON `{files: [{path, data(base64)}]}`）。可以多一层顶层目录；名字取 frontmatter；同名整个替换；
  `__MACOSX` 与点开头的文件、目录（`.DS_Store`、`.git/`、`.gitignore`）忽略，越界的路径拒绝。页面上只展示名字与描述，不在线编辑。
  frontmatter 不引 YAML 库，认真实技能包常见的写法：单行（可带引号）、`>` / `|` 块、缩进续行，只取顶层的 `name` 与 `description`，
  容忍 BOM 与 CRLF（`skills.parse`）；`description` 最长 2048 字符（标准建议 1024，官方技能里有超出一点的）。
- 名字：小写字母、数字、`-`，不以 `-` 开头结尾，≤64；单个技能 ≤8 MB，一轮合计 ≤16 MB，最多 50 个（`models.SKILL*`）。
- 网络自己写的 `SKILL.md`（提议、在线保存）frontmatter 只有 `name`、`description`（JSON 转义），正文是做法。附带文件不能叫 `SKILL.md`、不能有点开头的路径段。
- **重名**：网络按 项目技能 > 连接器自带 > 我的技能 合并进工作目录的 `skills/`（只读副本，运行后丢弃），
  被盖住的发 `skills.shadowed` 事件，只给 Agent 所有者。私有目录里的 `agent-home/skills` 作为第二个技能根排在后面，
  网络不读它，重名时运行时先找到的是 `skills/` 里的。
- **云端 dsh**：覆盖层**总是**写 `skill-filesystem`：`includeDefaultRoots: false`，`customSkillDirs` 只有上面两处。
  不读部署机器上的 `~/.dsh/skills`、`~/.agents/skills` 和数据目录所在仓库的 `.agents/skills`
  （`test_dsh_overlay_never_reads_default_skill_roots`）。
- **我的电脑**：技能随 `run.request.materials` 下发到 `skills/`。dsh 驱动同样关掉默认根；Claude Code 驱动把 `skills/`
  和 `agent-home/skills` 拷进工作目录的 `.claude/skills/`（网络下发的优先）；Codex 不会自己找技能（能力报告 `skills: "prompt"`），
  提示词里列出名字、描述与路径，正文让它按需读。列出的内容只在变化时重发（`Seat/Task.abilities_seen`）。
- 档案推送（`format_archive`）里列出项目技能的名字与描述；`project_detail.skill_list` 带作者（人或提议的 Agent）。

## 连接器

一个**连接**（`state.connections`，`models.Connection`）是某个人对一个连接器的一次授权，或者他电脑上已经配好的一个服务：
`{id, owner_id, connector_id, name, where: "cloud" | <device_id>, harness, status: ok | error | missing, error}`，不含凭证。
Agent 勾选在 `Agent.connections`，只能勾自己的、位置对得上的（云端连接给云端 Agent；电脑上的给同一台电脑、同一个 AI 工具上的 Agent）。

**一轮的有效连接器** = 勾给这个 Agent 的 ∩ 状态正常 ∩ 位置对得上 ∩ 这一轮的有效权限里有 `connector.use`
∩（在共享项目里，包括自己主持的）所有者在这个项目里没选“不行”的（见下面“共享项目里的授权”）。
委托不授予 `connector.use`，下游这一轮就没有（不变量 1、3）。有变化时提示词里按名字列出（`protocol.format_abilities`），
用到时要先问所有者的，名字后面注明“第一次调用时网络先问你的所有者”。

### 云端

- 目录：部署方维护的 `connectors.json`（默认 `<data>/connectors.json`，`--connectors` / `MATRIX_CONNECTORS`），
  按修改时间热加载，写坏了沿用上一份（`connector_catalog.py`）。目前只支持远程 MCP（Streamable HTTP）；
  `auth.fields` 描述授权时填什么、怎么拼成请求头。API（`/api/connectors`）不返回地址。
- 授权：`POST /api/me/connections`，网络用填的值试连一次（`tools/list`），结果写进 `status` / `error`；
  凭证只进 `vault/<连接 id>.bin`（Fernet 加密，0600），主密钥取 `MATRIX_VAULT_KEY` 或 `<data>/vault.key`（0600，首次生成）。
  新授权默认勾给助理。撤销删掉保管项与所有勾选。
- 调用：运行时挂的是本地 stdio 桥 `mcp_connectors.py`（MCP 服务名 `connectors`，工具名 `<连接器>__<工具>`），
  只带回调地址与执行上下文令牌。网络按令牌查这一轮的有效连接器，带上凭证转发（`mcp_client.RemoteMCP`），
  工具列表缓存 5 分钟。调用不在有效集合里的工具被拒绝，记 `authority.violation` 与 `task.violations`（不变量 10、11）。
  每次调用记进 `connections/<id>/calls.jsonl`（工具名、成败、耗时，不含参数与结果），`GET /api/me/connections/<id>/calls` 只给授权人。
- 桥连不上网络时 `tools/list` 为空、`tools/call` 回错误，不让整轮起不来（`failOnStartupError: false`）。

### 自定义（云端）

连接器是外部服务，MCP 是它的一种**接入方式**。自定义连接器目前只有这一种：远程 MCP（Streamable HTTP），由网络代连；
界面上写“接入方式：MCP”，配置手工写、可以再改，不把“连接器”说成“MCP”。

- **配置**：认常见 MCP 客户端的 `mcpServers` 写法（`mcp_client.parse_server_config`）：`{"mcpServers": {"名字": {...}}}`、
  `{"名字": {...}}` 或直接一项 `{"type": "http", "url": ..., "headers": {...}}`，一次一个服务，名字没单独填就用服务名。
  `type` 写 `http` / `streamable-http` 或不写；`sse` 和本机启动的（`command` / `stdio`）拒绝并说明为什么
  （本机的 MCP 配在电脑上的 AI 工具里，由 Matrix 客户端上报）。请求头最多 10 个，名字只能是字母、数字、`-`。
- `POST /api/me/connections/custom`：`{name?, config, agent_ids?}`（旧写法 `{name, url, header?, value?}` 照收）。
  `PUT /api/me/connections/<id>/custom`：`{name?, config}` 改配置。
- 地址只能是 `https://` 的**公网**地址：解析出的每个地址都要 `is_global`，连接时直连校验过的 IP（防 DNS 重绑定），
  不跟重定向，不走代理（`mcp_client.check_public_url` / `public_only`）。地址存在 `Connection.url`，
  请求头名与值只进保管库（`{"headers": {...}}`；旧的 `{header, value}` 照读）。
- 回显：`config` 里请求头的值一律是 `<已保存>`（`Network.KEEP_SECRET`）；改配置时原样交回就沿用保管库里的，
  写新值就替换，新加的请求头写 `<已保存>` 会被拒绝。目录里的连接器没有 `config`（地址不给用户看）。
- id（也是工具名前缀 `<id>__<工具>`）是 `custom-<名字>`，和自己的其他连接重了就加随机后缀（中文名都会落到 `custom-mcp`）。
  每人最多 20 个。

### 我的电脑

- Matrix 客户端上线时在每个 AI 工具的能力报告里带 `connectors`（只有名字，≤100 个）：dsh 读 `--dump-config` 里的
  `dsh-mcp-client` 块，Claude Code 读 `~/.claude.json` 的 `mcpServers`，Codex 读 `codex mcp list --json`（只算启用的）。
  网络据此生成连接（id 由设备、工具、名字决定），新出现的默认勾给那台电脑上的 Agent，没再报的标 `missing`。旧客户端不报，网络不动。
- `run.request.connectors` 是这一轮允许的服务名；驱动只开这些：Claude Code 用 `--strict-mcp-config` 加临时配置文件
  （只含网络工具与允许的服务），Codex 用 `-c mcp_servers.<名字>.enabled=false` 关掉其余的，dsh 在覆盖层里禁用其余的插件。
  没有这个字段（旧服务端）时驱动不干预。能力报告里 `connector.use` 不是 `enforced` 的工具，Agent 的有效权限里就没有它。
- 迁移：有连接器之前，电脑上的 Agent 一直能用本机工具里配的服务；`Store._load` 给没有 `connections` 字段的端侧 Agent 补上
  `connector.use`，只做一次。

### 共享项目里的授权：用到时先问（不变量 19）

共享项目里，别人 @ 我的 Agent，动的却是我的账号，读到的内容还会进大家看得到的群聊。所以每个连接器在每个共享项目里
有一个只属于授权人的授权（`Project.connector_grants`：`{agent_id: {connection_id: always | deny}}`，没写的就是 `ask`）：

| 授权 | 这一轮 |
| --- | --- |
| `ask`（默认，“用到时问我”） | 工具照常列出；这一轮第一次调用时 `connector_call` 先停下，给授权人一张申请卡片（`Approval.kind = "connector"`，`reason` 写明谁让哪个 Agent 用什么、回复会被谁看到），阻塞到决定或 `Limits.connector_ask_timeout_s`（540 秒，短于连接器桥的回调超时）。**只这一次**：这一轮都可以；**这个项目里都可以**：写成 `always`；**不行** / 超时：这一轮不再问，调用都回“没有同意”（收窄，不记违规）。同一轮同一个连接器并发调用只出一张卡片 |
| `always`（“这个项目里都可以”） | 直接用 |
| `deny`（“不行”） | 这一轮没有这个连接器 |

- 只有授权人能改（`PUT /api/projects/<id>/grants`），`project_detail.connector_grants` 只给授权人自己的那几项。
  只有自己的项目不需要授权：那里只有自己，勾给 Agent 的就能用。
- 电脑上的服务由那台电脑上的 AI 工具直接调用、网络拦不到，没有“用到时问”：只有 `always` 才放进 `run.request.connectors`。
- 读到的内容不另记信息流来源：调用之前已经问过授权人，卡片上写着回复会给谁看（不变量 22）。

### 项目的连接器面板

项目页的“连接器”标签：这个项目用到了哪些外部服务、每个成员能不能用。项目不持有凭证，也没有“公共授权”。
`project_detail.connector_panel = {services, unrecognized}`，每个服务一行，来源有四种：

| 来源 | 怎么来 | 怎么去掉 |
| --- | --- | --- |
| `ref` | 资料里的链接：主持人指定的（`ResourceRef.connector_id`，`PUT .../refs/<ref>/connector`），或者域名对上了连接器目录里的 `domains` | 删掉那条链接 |
| `plugin` | 启用的插件清单里的 `connectors`（只是声明） | 停用或卸载插件 |
| `used` | 这个项目里实际调用过（`Project.connector_usage`：次数、最近时间、哪个 Agent；不记账号）。只有网络代调用的云端连接器会记 | 主持人收起（`DELETE .../connector-usage/<连接器>`），再调用会重新出现 |
| `manual` | 主持人手动添加（`Project.connector_needs`：连接器、用途），群聊里有公开记录。三种：目录里的连接器、电脑上配好的服务名、自定义连接器（见下） | 主持人删掉 |

- 每一行的 `members` 按成员 Agent 给就绪状态：`ready` / `ask`（用到时问所有者）/ `not_enabled`（授权了但没勾给它，或所有者选了不行）/
  `unauthorized`，不给是谁的账号、哪个连接。`mine` 是看的人自己的 Agent 对这个服务的授权（只在共享项目里有），在面板里直接改。
- `unrecognized`：认不出服务的 http(s) 链接，主持人可以指定用哪个连接器打开。多数网页不需要。
- **项目里的自定义连接器**：主持人手工写 MCP 配置（同上面的写法），存进 `ConnectorNeed.name/url/headers`，
  所有成员都看得到（行里的 `custom = {need_id, access: "mcp", config, vars}`）。项目不持有凭证，所以**请求头的值只能是模板**：
  至少一个 `${变量}`，去掉变量后只剩不超过 16 个字母或空格（如 `"Bearer ${TOKEN}"`），写了真实值的拒绝且报错里不带它。
  地址写的时候只查写法（`check_url_shape`，不解析域名），成员连接时再全查。id 是 `custom-<名字>-<随机>`，不会和成员自己的撞上。
  - 成员连接（`POST .../needs/<need>/connect`，`{values: {变量: 值}, agent_ids?}`）：变量要填全，填进模板得到请求头，
    建的是**这个人自己的**连接（`connector_id` 沿用项目里的，凭证进保管库），默认勾给他在这个项目里的云端 Agent；
    再连一次就是换成现在的地址与凭证。之后和别的云端连接一样：共享项目里“用到时先问”。
  - 就绪状态只认连的是项目里写的那个地址的；主持人改了地址（`PUT .../needs/<need>`），成员原来的连接对不上，显示“还没连”，
    群聊里提示要重新连接。
- 要用别人的连接器，就委托给别人的 Agent（不变量 16、19）。
- Agent 发现缺连接器时调用 `suggest_connector`：只给它的所有者发一条提醒，不授权任何东西。Agent 没有安装、授权、开启连接器的工具。
- 迁移（`store._connector_panel`）：旧的 `connections_enabled`（所有者逐个打开的）改成 `always`；关联了资源引用的“需要的连接器”
  并进那条链接（`connector_id`，用途补进链接说明），没关联的留作手动添加。

## 接口

| 路由 | 用途 |
| --- | --- |
| `GET /api/me/skills`、`POST /api/me/skills/upload`、`DELETE /api/me/skills/<名字>` | 我的技能库 |
| `POST /api/projects/<id>/skills/upload`、`DELETE .../skills/<名字>` | 项目技能（主持人上传，成员读） |
| `POST /api/me/connections/custom`、`PUT /api/me/connections/<id>/custom` | 自定义连接器（接入方式：远程 MCP，只连公网）：添加、改配置 |
| `GET /api/connectors` | 云端连接器目录（不含地址） |
| `GET/POST /api/me/connections`、`POST .../<id>/check`、`DELETE .../<id>`、`GET .../<id>/calls` | 我的连接 |
| `POST /api/projects/<id>/needs`、`DELETE .../needs/<need>` | 连接器面板里手动添加（主持人）：`{connector_id, purpose}`，或自定义连接器 `{name?, config, purpose}` |
| `PUT /api/projects/<id>/needs/<need>` | 改项目里自定义连接器的 MCP 配置（主持人） |
| `POST /api/projects/<id>/needs/<need>/connect` | 成员按项目里的自定义连接器用自己的凭证连接 |
| `PUT /api/projects/<id>/refs/<ref>/connector` | 指定一条链接用哪个连接器打开（主持人；空字符串回到按域名认） |
| `DELETE /api/projects/<id>/connector-usage/<连接器>` | 收起“用过”的记录（主持人） |
| `PUT /api/projects/<id>/grants` | 共享项目里我的连接器怎么用：`{agent_id, connection_id, grant: ask \| always \| deny}` |
| `POST /api/approvals/<id>` 的 `scope` | 连接器申请同意的范围：`once`（这一轮）/ `project`（这个项目里都可以） |
| `PATCH /api/agents/<id>` 的 `skills`、`connections` | Agent 勾选 |
| `/internal/connectors/list`、`/internal/connectors/call` | 运行时回调，只凭执行上下文令牌 |

事件：`skills.updated`、`connection.updated` 只推给本人；`skills.shadowed`、`connector.suggested` 只推给 Agent 所有者。
