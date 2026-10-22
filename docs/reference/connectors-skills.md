# 连接器与技能

> 入口见 [AGENTS.md](../../AGENTS.md)。设计与取舍见 [docs/connectors-and-skills.md](../connectors-and-skills.md)。
> 代码：`engine.py`「连接器与技能」一节、`skills.py`、`connector_catalog.py`、`vault.py`、`mcp_client.py`、`mcp_connectors.py`。

两个并列的用户概念：**连接器**是外部服务的接入，要授权，动的是授权人的状态，默认隔离；**技能**是一种做法
（`SKILL.md` + 可选文件），不要授权，是能力，可以共享。个人层管“我有什么”，项目层管“这里需要什么、这里怎么做”。

## 技能

| 层 | 在哪 | 谁写 | 谁能看 |
| --- | --- | --- | --- |
| 项目技能 | `projects/<id>/skills/<名字>/`，元数据在 `Project.skills` | 主持人（UI）或经主持人批准的 `propose_skill` 提议 | 项目里所有人（档案的一部分，不变量 17） |
| 连接器自带的技能 | `cache/connector-skills/<连接器>/`，从 `connectors.json` 生成 | 部署方 | 用到这个连接器的那一轮 |
| 我的技能 | `users/<uid>/skills/<名字>/`；Agent 勾选在 `Agent.skills` | 本人 | 只有本人（不变量 13）；勾上的跟着 Agent 进联合项目 |
| Agent 自己攒的 | 私有目录里的 `agent-home/skills/` | Agent 自己 | 网络不读（不变量 8） |

- 名字：小写字母、数字、`-`，不以 `-` 开头结尾，≤64；单个技能 ≤256 KB，一轮合计 ≤2 MB，最多 50 个（`models.SKILL*`）。
- `SKILL.md` 的 frontmatter 只有 `name`、`description`（JSON 转义），正文是做法。附带文件不能叫 `SKILL.md`、不能有点开头的路径段。
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
∩（在联合项目里，包括自己主持的）所有者在这个项目里打开了的（`Project.connections_enabled`，只有所有者能改、只有所有者看得到）。
委托不授予 `connector.use`，下游这一轮就没有（不变量 1、3）。有变化时提示词里按名字列出（`protocol.format_abilities`）。

### 云端

- 目录：部署方维护的 `connectors.json`（默认 `<data>/connectors.json`，`--connectors` / `AGENTNET_CONNECTORS`），
  按修改时间热加载，写坏了沿用上一份（`connector_catalog.py`）。目前只支持远程 MCP（Streamable HTTP）；
  `auth.fields` 描述授权时填什么、怎么拼成请求头。API（`/api/connectors`）不返回地址。
- 授权：`POST /api/me/connections`，网络用填的值试连一次（`tools/list`），结果写进 `status` / `error`；
  凭证只进 `vault/<连接 id>.bin`（Fernet 加密，0600），主密钥取 `AGENTNET_VAULT_KEY` 或 `<data>/vault.key`（0600，首次生成）。
  新授权默认勾给助理。撤销删掉保管项与所有勾选。
- 调用：运行时挂的是本地 stdio 桥 `mcp_connectors.py`（MCP 服务名 `connectors`，工具名 `<连接器>__<工具>`），
  只带回调地址与执行上下文令牌。网络按令牌查这一轮的有效连接器，带上凭证转发（`mcp_client.RemoteMCP`），
  工具列表缓存 5 分钟。调用不在有效集合里的工具被拒绝，记 `authority.violation` 与 `task.violations`（不变量 10、11）。
  每次调用记进 `connections/<id>/calls.jsonl`（工具名、成败、耗时，不含参数与结果），`GET /api/me/connections/<id>/calls` 只给授权人。
- 桥连不上网络时 `tools/list` 为空、`tools/call` 回错误，不让整轮起不来（`failOnStartupError: false`）。

### 我的电脑

- Matrix 客户端上线时在每个 AI 工具的能力报告里带 `connectors`（只有名字，≤100 个）：dsh 读 `--dump-config` 里的
  `dsh-mcp-client` 块，Claude Code 读 `~/.claude.json` 的 `mcpServers`，Codex 读 `codex mcp list --json`（只算启用的）。
  网络据此生成连接（id 由设备、工具、名字决定），新出现的默认勾给那台电脑上的 Agent，没再报的标 `missing`。旧客户端不报，网络不动。
- `run.request.connectors` 是这一轮允许的服务名；驱动只开这些：Claude Code 用 `--strict-mcp-config` 加临时配置文件
  （只含网络工具与允许的服务），Codex 用 `-c mcp_servers.<名字>.enabled=false` 关掉其余的，dsh 在覆盖层里禁用其余的插件。
  没有这个字段（旧服务端）时驱动不干预。能力报告里 `connector.use` 不是 `enforced` 的工具，Agent 的有效权限里就没有它。
- 迁移：有连接器之前，电脑上的 Agent 一直能用本机工具里配的服务；`Store._load` 给没有 `connections` 字段的端侧 Agent 补上
  `connector.use`，只做一次。

### 项目需要的连接器

- 主持人声明（`Project.connector_needs`：连接器、用途、可选的资源引用），群聊里有公开记录。项目不持有凭证，也没有“公共授权”。
- `project_detail.connector_readiness` 按成员给就绪状态：`ready` / `not_enabled`（授权了但没在这个项目打开）/ `unauthorized`，
  不给是谁的账号、哪个连接。要用别人的连接器，就委托给别人的 Agent（不变量 16、19）。
- Agent 发现缺连接器时调用 `suggest_connector`：只给它的所有者发一条提醒，不授权任何东西。Agent 没有安装、授权、开启连接器的工具。

## 接口

| 路由 | 用途 |
| --- | --- |
| `GET/PUT /api/me/skills`、`GET/DELETE /api/me/skills/<名字>`、`POST .../files` | 我的技能库 |
| `GET/PUT /api/projects/<id>/skills`、`GET/DELETE .../skills/<名字>`、`POST .../files` | 项目技能（主持人写，成员读） |
| `GET /api/connectors` | 云端连接器目录（不含地址） |
| `GET/POST /api/me/connections`、`POST .../<id>/check`、`DELETE .../<id>`、`GET .../<id>/calls` | 我的连接 |
| `POST /api/projects/<id>/needs`、`DELETE .../needs/<need>` | 项目需要的连接器 |
| `PUT /api/projects/<id>/members/<agent>/connections` | 联合项目里打开我的连接器 |
| `PATCH /api/agents/<id>` 的 `skills`、`connections` | Agent 勾选 |
| `/internal/connectors/list`、`/internal/connectors/call` | 运行时回调，只凭执行上下文令牌 |

事件：`skills.updated`、`connection.updated` 只推给本人；`skills.shadowed`、`connector.suggested` 只推给 Agent 所有者。
