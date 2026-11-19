# 外部应用接入（设计）

> 状态：**第 1–3 期已实现**，不变量 30，实现说明见第 12 节；第 4 期（SSO）随登录改造。需求见 [REQUIREMENTS.md](../REQUIREMENTS.md) #73。相关设计：[project-services.md](project-services.md)（项目服务，不变量 29）、
> [public-agents.md](public-agents.md)（不变量 27）、[remote-agents.md](remote-agents.md)（标准 HTTP 形状，不变量 28）、[blueprints.md](blueprints.md)（模板）。
> 调研：research: project-publishing#external-apps。
> 业务系统怎么对接（拿令牌、调接口、检查清单）见 [open-api-integration.md](open-api-integration.md)。

## 1. 要解决什么

企业里有很多业务系统（报销、CRM、工单……）。它们要比“调用 Matrix 的服务”更深入地接进来：

1. **查**：查到能用的 Agent；
2. **组**：用这些 Agent 组成项目（直接建，或用模板建）；
3. **发**：把项目作为服务发布出去；
4. **调**：调用服务（自己的或别人的）。

## 2. 已定的事（2026-11-18）

| # | 决策 |
| --- | --- |
| 1 | 新增**外部应用**：只有**管理员**能登记 |
| 2 | 两种身份都支持：**代表一个人**（委托授权）与**业务系统账号**（系统自己的身份） |
| 3 | 系统账号的**负责人**在网页上等同主持人、能看内容；**令牌**只给配置与用量，不给内容 |
| 4 | “查”的范围：公共 Agent、授权人自己的 Agent、所有者**主动开放**给这个应用的 Agent；只给名片 |
| 5 | “组”只**组合现有的 Agent 与用模板**，不新建 Agent |
| 6 | 开放给应用的个人 Agent 被拉进项目时，**所有者自动成为参与者**：等价于受邀加入、再把自己的 Agent 带进来，现有规则不改 |
| 7 | 应用自己调用服务时拿到回复；它发布的服务被别人用时**只看用量**（同不变量 29） |
| 8 | 系统账号的连接器由**负责人代为授权**，**管理员也能配** |
| 9 | 代表个人时，令牌管**经这个应用建的项目**，加上授权人在授权时**另外交给它的**、自己主持的已有项目 |
| 10 | 调用服务只凭**应用令牌**（`services.call`），不另发服务密钥：接入方都先由管理员登记成应用，谁在调用一目了然 |

## 3. 两种身份

接口一样，令牌代表的主体不同：

| | 代表一个人 | 业务系统账号 |
| --- | --- | --- |
| 令牌代表谁 | 授权它的那个人 | 系统账号自己 |
| 谁签 | 用户在“我的授权”里签 | 管理员在后台签 |
| 项目归谁 | 那个人当主持人 | 系统账号当主持人 |
| 适合 | 个人用工具帮自己搭项目 | 长期存在、不随某个人离职失效的系统 |

### 系统账号

- 用户表里一种账号，`User.kind = "system"`：没有密码、不能登录网页，只凭令牌调接口；由管理员创建，至少一名**负责人**（真人）。
- 在网络里的地位和用户一样：能当主持人、当服务的调用方、有自己的连接器授权、用量台账里单独一行。归属、可见性、
  不变量 16 与 29 都不用另写。
- **不能新建 Agent**（决策 5）：它的项目成员只来自公共 Agent 与开放给它的 Agent。
- **负责人**在网页上看到系统账号主持的项目，权限同主持人（看群聊与档案、改配置、撤回服务、处理委托的审批），按他自己的身份留痕；
  项目页写明“主持人：报销系统（负责人 maya、ben）”。管理员增减负责人；最后一名负责人不能移除。
- **连接器**：负责人在“连接器”页代系统账号授权（凭证只进保管库），管理员在后台也能配。系统账号调用服务时，业务授权用的就是这些。

### 代表一个人

- 用户在“我的授权”页选一个已登记的应用，勾选范围（scope），可以再限定到某几个项目或“只管它自己建的项目”，签发令牌。
- 应用做的事等于这个人本来就能做的事，再交 scope 与项目范围。撤销后令牌立即作废，它建的项目、发布的服务都留着，归这个人。

## 4. 四层能力

| 层 | 应用能做什么 | scope | 依据 |
| --- | --- | --- | --- |
| 查 | 列出可用的 Agent 与模板（名片：名称、描述、能力、权限、要用的连接器、在不在线） | `agents.read` | 名片可见性同 `can_see_agent`；别人的个人 Agent 只有开放给这个应用的才看得到 |
| 组 | 建项目（直接建，或用模板建）；增减成员；纲要、角色；上传资料、设可见范围；项目技能；只从部署方目录装插件；删经它自己建的项目（交给它管的已有项目只能管、不能删） | `projects.write` | 以主持人身份操作；拉成员同 `can_call_agent`，加上开放给它的 Agent |
| 发 | 在它主持（或被允许管）的项目里发布、修改、撤回服务；看服务用量 | `services.publish` | 不变量 29 |
| 调 | 以自己的身份调用服务（见第 6 节） | `services.call` | 不变量 29；外部任务不带进内部权限 |

**令牌拿不到**：群聊、项目会话、服务会话的内容（它自己调用服务的那些会话除外），委托过程，私有目录，任何人的凭证。
项目建好以后照常运转：人和 Agent 在里面 @、委托、验收，应用不参与。

### 开放给应用的 Agent

- 所有者在自己 Agent 的设置里选“开放给应用”，勾哪些应用（或系统账号）。之后这些应用“查”得到它、能把它拉进自己主持的项目。
- 拉进去时，所有者**自动成为那个项目的参与者**（决策 6）：项目出现在他的项目列表里，他看得到群聊与档案；Agent 以他的名义、
  用他的资源运行，审批、提问照常找他。和受邀加入后带进自己的 Agent 一样。
- 所有者退出项目，他的 Agent 一起退出；取消开放，只是之后不能再被拉进新项目（已经在的项目照常，要退出就退出项目）。

## 5. 接口

REST，`Authorization: Bearer mat_…`（应用令牌），不走登录（Caddy 里和 `/mcp/remote` 一样不加 basic auth）：

```
GET    /open/v1/agents                              能用的 Agent（名片）                     agents.read
GET    /open/v1/blueprints                          能用的模板                               agents.read
GET    /open/v1/projects[/{id}]                     它能管的项目（配置，不给内容）           projects.write 或 services.publish
POST   /open/v1/projects                            建项目 {name, description, brief, members[]}，
                                                    可再带 {blueprint, params, roles: {角色: 成员 id}, digest}   projects.write
PATCH  /open/v1/projects/{id}                       描述、纲要、角色                         projects.write
DELETE /open/v1/projects/{id}                       删项目：只能删经这个应用建的             projects.write
PUT    /open/v1/projects/{id}/members/{agent}       拉进来（开放的 Agent：所有者先成为参与者）；DELETE 移出
PUT    /open/v1/projects/{id}/files/{path}          上传资料（请求体就是文件，≤5 MB）
PATCH  /open/v1/projects/{id}/access                {path, rule} 可见范围
POST   /open/v1/projects/{id}/skills                {name, description, body} 项目技能
POST   /open/v1/projects/{id}/plugins               {name, digest?} 只从部署方目录装
POST   /open/v1/projects/{id}/services              发布服务；PATCH / DELETE …/services/{sid}；GET …/usage   services.publish
GET    /svc/agents/{service}                        服务名片                                 services.call
POST   /svc/agents/{service}/chat                   {message, session_id?} → SSE             services.call
POST   /svc/sessions/{session_id}/interrupt         中断                                     services.call
```

- 要确认的事（模板、插件）第一次返回 409 和 `{confirm, digest}`，带上 `digest` 再来一次才装：和网页上的确认卡一样，先看再装。
- 认不出、撤销、停用的令牌 401；没有那个 scope 403；每把令牌每分钟最多 600 次（429）。

## 6. 调用服务

就是项目服务的第二期（[project-services.md](project-services.md) 第 6 节）：标准 HTTP 形状 `/svc/agents/{service}/chat`，
`{service}` 是服务 id。流里先是 `session`（会话 id 就是这个服务会话的对话 id，续接时带回来），之后 `delta`（入口这一轮说的话，
以及委托出去还在等时的中途回复）、`tool`（只给工具名与一行摘要）、最后 `done`（这一串做完时的最终回复）或 `error`。
和外部平台的标准形状一字不差：另一个 Matrix 用内置的 `standard` 驱动、`url` 填 `…/svc`、API Key 填应用令牌就能把服务接成外部平台 Agent。
调用方是令牌代表的主体：系统账号调用时，调用方就是这个系统账号，业务授权用系统账号自己的连接器（第 3 节）；代表一个人时是那个人。
SSO 改造以后，系统账号还可以带上终端用户的身份，按那个用户调用（随登录改造一起定）。

## 7. 留痕

应用的每次写操作记进档案日志：`by_user` 是令牌代表的主体（授权人或系统账号），`via_app` 是应用 id；群聊里的事件写成
“maya 经「报销系统」发布了服务…”。管理后台的“外部应用”页看每个应用的调用次数、最近操作与它主持的项目（只看计数与类别）。

## 8. 不变量 30

**外部应用只以授权它的人或它自己的系统账号的名义、在授权范围内操作**：做的事等于那个主体本来就能做的、再交令牌的 scope 与项目范围；
令牌拿不到内容（对话、过程、私有目录、凭证；它自己调用服务的会话除外）；每次写操作都留下“经哪个应用”；撤销立即生效。
系统账号不能登录网页、不能新建 Agent、至少一名负责人；开放给应用的个人 Agent 被拉进项目时，所有者成为参与者。

## 9. 分期

1. **应用与身份（已实现）**：应用登记（管理后台）；两种令牌；系统账号与负责人；“外部应用”页（我的授权、我负责的系统账号）；开放 Agent 给应用；查。
2. **组与发（已实现）**：建项目（直接建 / 用模板）、成员、纲要、角色、资料与可见范围、技能、目录插件；服务的发布与用量；经应用留痕。
3. **调（已实现）**：`/svc/*` 标准 HTTP 形状，凭应用令牌。
4. **SSO**：随登录改造。
5. 可选：管理能力包成 MCP 服务，让外部平台上的 Agent 也能组项目。

## 10. 待定

1. **负责人交接**：负责人离职时管理员换人（已能换）；系统账号停用时它主持的项目怎么办（只读、转交给某个负责人）。

## 11. 暂不做

- **平台 Agent 的审批与提问**（2026-11-18 用户定先不管）：现在回不到 Matrix（远程运行时没有）。以后要补，可以在 `/mcp/remote` 开
  `request_approval` / `ask_user`，审批照 Matrix 的规则发（个人 Agent 给所有者，公共 Agent 与服务里给调用方），只能算 `advisory`。

## 12. 实现说明（2026-11-18）

| 部分 | 在哪里 |
| --- | --- |
| 模型 | `models.ExternalApp`、`AppGrant`（令牌只存 sha256）、`User.kind / managers / app_id`、`AgentNode.open_to`、`Project.via_app`、`APP_SCOPES` |
| 引擎 | `matrix/apps.py`（`AppsMixin`，混入 `Network`）：管理员（`create_app` / `update_app` / `issue_system_token`）、用户（`apps_for` / `grant_app` / `revoke_grant` / `set_agent_open`）、负责人（`system_connections` / `authorize_system_connector`）、令牌（`app_context`：认令牌、查 scope、限频）、查组发（`open_*`，项目范围 `_app_project`；`open_delete_project` 只删 `via_app` 是自己的）、调（`svc_card` / `svc_session` / `svc_interrupt`）；留痕 `via_app()`（档案日志 `via_app`、群聊事件后缀“（经「应用」）”） |
| 引擎里改的地方 | `hosted_project` / `is_host` 认负责人；`is_admin` 不认系统账号；`invite` 不邀请系统账号；`project_view` 写“应用名（负责人 …）”；`auth.login` / `issue_reset` 拒绝系统账号 |
| 接口 | `matrix/open_server.py`（`/open/v1/*`、`/svc/*`）；`server.py` 的 `/api/admin/apps*`、`/api/apps*`、`/api/grants/*`、`/api/agents/{id}/open`、`/api/system-accounts/*` |
| 界面 | 管理后台“外部应用”（登记、签 / 重签系统账号令牌、负责人、停用）；“更多 → 外部应用”（我授权的应用、签令牌、我负责的系统账号与它的连接器）；Agent 设置里的“开放给应用” |
| 测试 | `tests/test_open_api.py`（含用真实 HTTP 服务与内置 `standard` 驱动接 `/svc`） |

**限制**：

- 用模板时，应用先建项目、再把模板装进去：模板里的每个角色都要指给 `members` 里的成员（不新建 Agent），模板里的项目名、
  `g.blueprint` 标记不写（装进已有项目的路径）。
- `/svc` 一次请求等到这一串做完（入口委托出去也等），最多 15 分钟；超时返回 `error`，带着 `session_id` 再问可以接着拿。
- 应用拿不到项目里的审批卡、提问卡：这些照常发给人（个人 Agent 的所有者、负责人、调用方本人）。
- 取消开放只影响之后：已经在项目里的照常，要退出就退出那个项目。
