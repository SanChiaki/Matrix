# 发布与订阅

> 入口见 [AGENTS.md](../../AGENTS.md)。设计与取舍见 [docs/project-publishing.md](../project-publishing.md)，不变量 23 全文见 [invariants.md](invariants.md)。
> 代码：`engine.py`「发布与订阅」一节、`releases.py`（快照）、`protocol.format_subscriptions`、`server.py` 的发布物路由、
> `static/app.js` 的“发布物”视图与项目页“发布”标签。示例：`examples/delivery-dashboard/`。

项目对外拿出的东西是**发布物**（`Release`，`state.releases`）：主持人从项目里选内容、定读者，固定成一个版本。
读者看的、他的 Agent 用的都是发布物，不是项目本身（不变量 23）。

## 两种发布物

| `kind` | 内容 | 读者 | 订阅后 |
| --- | --- | --- | --- |
| `method`（技能包） | `skills`：项目技能或启用的插件自带的技能（重名时项目技能赢，`_release_skill_sources`） | `readers`，或 `public`（所有登录用户） | 进订阅者 Agent 的技能层：项目技能 > 插件自带的技能 > **订阅的技能包** > 连接器自带 > 我的技能 |
| `fact`（交付件） | `files`：档案文件；可带 `view: {plugin, tab}` | 只有 `readers`（`public` 对交付件无效） | 只读拷进工作目录 `releases/<name>/`，运行后丢弃；端侧随材料下发 |

项目参与者总是看得到（含撤回的记录）；读者与公开技能包的其他人只看得到在发布中的。看不到的一律 404（`visible_release`）。
读者看不到项目、群聊和档案里没发布的东西。

## 发布、版本、撤回

- **只有主持人**（`hosted_project`）：`create_release`、`update_release`（标题、说明、读者、`public`、`follow`；改了内容就切新版本）、
  `publish_release_version`（按档案现在的样子切一版）、`withdraw_release`。Agent 不能发布。
- 名字同技能规则（`releases.check_name`），项目内在发布中的不重名；每个项目 ≤20 份，每份 ≤30 项、≤200 个读者（`models.RELEASE*`）。
  读者按邮箱填，项目参与者不必点名。
- 每个版本是一份快照 `releases/<id>/v<N>/`（`skills/`、`files/`、`page/`、`manifest.json`），先写临时目录再改名，保留最近 3 个版本。
  带插件视图的，快照里有发布时那一版插件的 `page/`。
- 发布、改读者、新版本、撤回都是档案变化：`archive_rev` +1、`log.jsonl` 记 `release.publish` / `release.update` / `release.withdraw`、群聊里一条公开说明（写明给谁看）。
- **跟随档案**（`follow`，只有交付件）：主持人开的长期许可。`publish_file`（人上传或 Agent 发布）更新了列出的文件，网络自动切新版本，
  `published_by` 记为主持人，群聊里记“跟随档案更新到 vN”。读者、文件清单的变化仍要主持人操作。
- 撤回：状态 `withdrawn`、快照删除、跟随关掉；订阅留着但不再生效。删除项目时它发出去的一并撤回。

## 订阅

- `AgentNode.subscriptions`：人给**自己的** Agent 勾（`set_subscription` 或 `PATCH /api/agents/<id>` 的 `subscriptions`），
  只能订阅所有者看得到、在发布中的（`_clean_agent_subscriptions`），每个 Agent ≤30 份。
- **生效条件**（`_subscriptions_for`）：发布中、所有者还看得到、并且这一轮输出能到达的人（`_reach`：项目里是全部参与者 + 所有者，
  私聊 / 助理对话只有所有者）都看得到它（`_release_audience`；公开的技能包对所有人成立）。不生效的写明原因。
- 提示词里的“## 订阅的发布物”（`format_subscriptions`）和连接器、技能一起只在变化时推送：生效的技能包列技能名，
  生效的交付件列文件路径与快照时间，没生效的只写名字和原因、“不要凭记忆复述”。
- 运行用订阅者自己的资源和授权：发布物只带内容，不带发布方的连接器或凭证。发布方只看得到订阅数（`subscribers`），看不到是谁、问了什么。

## 发布物里的插件视图

- 插件在清单 `release_views` 里声明能在发布物里打开的标签（`plugins.parse_manifest` 校验它们在 `tabs` 里）。
- 读者打开：`POST /api/releases/<id>/frame` 签一个令牌（`_sign`，`k: "release"`，绑定 读者 + 发布物 + 版本，10 分钟），
  页面从 `/release-frame/<令牌>/page/...` 取，只能取快照 `page/` 下的文件和 `_matrix/sdk.js`；响应头同插件页面的 CSP（不变量 20）。
  换了版本、撤回、读者被移出名单，旧令牌作废。插件页面的令牌不能拿来取发布物，反之亦然。
- 桥（`release_bridge`）只有三个只读操作：`context`（`mode: "release"`、发布物信息与快照时间）、`archive.read`（这个版本的文件清单）、
  `archive.file`（读其中一个文本文件）。记录、群聊、成员、以人名义的动作一律没有。父页面处理 `ui.badge`（忽略）与 `ui.file`（预览快照里的文件）。

## 反馈

`release_feedback`：看得到发布物的人写一段（≤2000 字），存进 `Release.feedback`（保留最近 200 条），在项目群聊里记一条事件
“读者 x 对发布物「…」vN 的反馈：…”，不唤醒任何成员；`log.jsonl` 记 `release.feedback`（不动 `archive_rev`）。
项目参与者看全部反馈，读者只看自己的。

## 接口

| 方法 | 路径 | 谁 |
| --- | --- | --- |
| GET | `/api/releases` | 登录用户：看得到的、在发布中的 |
| GET | `/api/releases/<id>` | 看得到的人；`readers`、`subscribers`、全部反馈只给项目参与者 |
| GET | `/api/releases/<id>/files/<name>` | 看得到的人；一律附件下载 |
| POST | `/api/releases/<id>/subscribe` `{agent_id, on}` | 给自己的 Agent 订阅 / 退订 |
| POST | `/api/releases/<id>/feedback` `{text}` | 看得到的人 |
| POST | `/api/releases/<id>/frame`、`/api/releases/<id>/bridge` | 看得到的人（带视图的交付件） |
| GET | `/release-frame/<令牌>/<path>` | 凭令牌 |
| POST | `/api/projects/<id>/releases` | 主持人：`{name, kind, title, description, skills | files, view, readers, public, follow}` |
| PATCH | `/api/projects/<id>/releases/<rid>` | 主持人 |
| POST | `/api/projects/<id>/releases/<rid>/publish` | 主持人：切新版本 |
| DELETE | `/api/projects/<id>/releases/<rid>` | 主持人：撤回 |

事件 `release.updated` 只带 `release_id`，按 `can_see_release` 推送。`project_detail` 带 `release_list`（这个项目发出的全部）与
`release_skills`（主持人能选的技能名）。

## 还没做

- 共享项目里“内容涉及谁就要谁同意”（第一版只要主持人，群聊公开记录）。
- 发布物目录的排序与信号（订阅数、新鲜度、反馈）、跨项目依赖与固定版本、对外（A2A）。
- 节律面：项目级职责清单、`ask_decision`、整理回合（见设计文档第 10 节）。
