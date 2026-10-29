# 发布与订阅（设计）

> 状态：第一期已实现（见第 11 节分期），当前行为见 [reference/releases.md](reference/releases.md)。项目模型见 [projects.md](projects.md)，
> 插件见 [project-plugins.md](project-plugins.md)，不变量见 [reference/invariants.md](reference/invariants.md)。
> 调研：research: project-publishing#three-faces 及文中各处引用。

## 1. 要解决什么

项目现在只有**作业面**：群聊、委托、档案、插件都给项目里的人和 Agent 用。项目里做出来的东西要给项目外的人用，
只有一条路——把人拉进项目，成为参与者。这条路太重：读者要的是结果，不是加入协作、看全部群聊。

一个项目有三个面（research: project-publishing#three-faces）：

| 面 | 回答什么 | 现在有什么 |
| --- | --- | --- |
| 作业面 | 里面的人和 Agent 怎么协作 | 群聊、委托、档案、项目技能、插件 |
| **发布面** | 对外拿出什么、给谁、多新 | 本文 |
| 节律面 | 没人 @ 时靠什么往前走 | 插件触发、主人的定时；职责清单见第 10 节（后续分期） |

**发布物**是发布面的单位：项目主持人从档案里选出一组内容，定好读者，固定成一个版本。项目外的人看的、
他的 Agent 用的，都是发布物，不是项目本身，也不是项目里的某个 Agent。

参照：数据网格"数据即产品"（可寻址、自描述、可信、可发现，research: project-publishing#data-as-product）；
Kylon App 的发布由人审核、可见性分档（research: kylon#apps）。

## 2. 两种发布物

律所类比："专业能力能跨客户，客户事实不能；从事实里抽象出方法需要一道显式步骤"
（research: agent-definition-context-boundaries#law-firm-analogy、research: project-publishing#method-vs-fact）。

| | 技能包（`method`） | 交付件（`fact`） |
| --- | --- | --- |
| 内容 | 一组技能：项目技能、插件自带的技能 | 一组档案文件，可带一个插件视图（插件页面，按插件而定：看板、表格、报告…） |
| 例子 | 交付数据的查数口径、做看板的技能 | 交付周报看板、分析结论 |
| 读者 | 名单，或部署里所有登录用户（`public`） | 只能是名单（加上项目参与者） |
| 订阅后 | 进订阅者 Agent 的技能层 | 只读挂到订阅者 Agent 的工作目录 `releases/<名字>/` |

技能包可以公开，因为它已经去掉了具体项目的数据（主持人发布时确认这一点）；交付件只能给点名的人。

名字：设计时按律所类比叫“方法 / 事实”，界面与文档统一叫**技能包 / 交付件**（2026-10-29 定），代码里的 `kind` 仍是 `method` / `fact`。
插件里的技能（以前叫“打法”）就叫技能，或“插件自带的技能”。

## 3. 发布物的形状

`Release`（`state.releases`）：

| 字段 | 说明 |
| --- | --- |
| `id`、`project_id`、`name` | `name` 规则同技能（kebab-case），项目内唯一 |
| `kind` | `method` \| `fact` |
| `title`、`description` | 给读者看的说明（自描述） |
| `skills` | method：技能名（项目技能或插件自带的技能） |
| `files` | fact：档案文件名 |
| `view` | fact 可选：`{plugin, tab}`，插件在清单 `release_views` 里声明能在发布物里打开的标签 |
| `readers` | 读者（用户 id）；`public` 只对 method 有效 |
| `follow` | fact：跟随档案。列出的文件在档案里一更新，网络自动切一个新版本（见第 4 节） |
| `version`、`published_at`、`published_by` | 当前版本 |
| `status` | `active` \| `withdrawn` |
| `feedback` | 读者的反馈（第 8 节） |

内容按版本拷进 `releases/<id>/v<N>/`（`skills/`、`files/`、`page/`、`manifest.json`），固定不变；保留最近 3 个版本。
读者看到、订阅者用到的永远是某个确定的版本，以及它的快照时间（数据网格的"新鲜度"）。

## 4. 发布：只经主持人

- **谁**：只有项目主持人发布、改读者、切新版本、撤回（`hosted_project`）。Agent 不能发布，技能里写"做好后请主持人发布"。
- **公开记录**：发布、新版本、撤回都是档案变化：`archive_rev` +1、写 `log.jsonl`、群聊里一条说明（写明给谁看）。
- **跟随档案**（`follow`）：主持人对一份交付件开的长期许可，范围固定为"这几个文件 + 这份读者名单"。
  名单内的文件被 `publish_file` 或人上传更新时，网络自动切新版本并在群聊里记一条；读者、文件清单变了都要主持人再操作。
  参照 Kylon 的 standing publish approval（research: kylon#apps）。没有跟随的，主持人点"发布新版本"才更新。
- **撤回**：读者与订阅者立即看不到、用不到；快照删除；记录保留（`withdrawn`）。
- **共享项目**：第一版只要主持人确认，群聊里公开记录，参与者看得到发出去了什么、给了谁。
  2026-10-29 定：发布由主持人手动决定、群聊公开记录即可，暂不做“内容涉及谁就要谁同意”、也不核对读者在数据源里的权限（发布交付件就是主持人替读者做了一次授权，由主持人负责；research: project-publishing#matrix-decision）。

## 5. 读者与可见性

- 看得到一份发布物的人 = 项目参与者 ∪ `readers` ∪（`public` 时所有登录用户）。其他人一律按不存在（404，不变量 13）。
- 撤回的只有项目参与者看得到记录。
- 读者看到的是发布物本身：说明、版本、文件、视图、自己的反馈；看不到项目、群聊、档案里没发布的东西。

## 6. 订阅：在订阅者自己的信任域里

不做"项目的一个公共 Agent 服务外人"：那是早先 public Agent 的问题（Agent 不跨信任域，不变量 16），
而且所有者能看自己 Agent 的全部过程，消费者问了什么发布方都会看到（research: project-publishing#consumer-instance、
research: project-publishing#gpts-builder-privacy）。

- **订阅**是人给**自己的** Agent 勾上一份看得到的发布物（`AgentNode.subscriptions`，同 `Agent.skills` 的勾选）。
  运行用订阅者自己的资源、自己的连接器授权；发布物只**声明**用到的连接器，从不带发布方的凭证
  （反例：research: workbuddy#connector-auth、research: claude-tag#field-reports）。
- **生效条件**：这一轮输出能到达的人（`_reach`）都看得到这份发布物，订阅才生效。
  在助理对话、私聊里只有所有者，总是生效；在共享项目里有不在读者名单上的人，这一轮就不挂，提示词里只写名字和"没挂上的原因"。
  这是不变量 22 的同一种判断：集合比较，不用模型。public 的技能包对所有人都生效。
- **技能包**：作为一层技能，排在项目技能、插件自带的技能之后，连接器自带与我的技能之前（重名时前者赢）。
- **交付件**：文件只读拷进工作目录 `releases/<名字>/`（端侧随材料下发），运行后丢弃；提示词列出名字、版本、快照时间。
  Agent 回答时带上快照时间（陈旧风险，research: project-publishing#risks）。
- 发布方看不到谁订阅了（订阅是消费者的私事）；只给项目参与者订阅数。
- 读者被移出名单或发布物撤回：订阅留着但不再生效，提示词里说明。

## 7. 查看：发布物页面与插件视图

- 导航里的"发布物"列出我看得到的发布物（我的项目发出的、给我的、公开的）。详情页：说明、版本与快照时间、
  文件预览、订阅开关（我的哪些 Agent）、反馈。
- **插件视图**：交付件可以带一个插件标签。发布时把插件的 `page/` 一起快照进这个版本（人看过的代码才在读者浏览器里跑）。
  读者打开时是同样的沙箱 iframe（不变量 20），令牌绑定 读者 + 发布物 + 版本；桥只有只读的三个操作：
  `context`（`mode: "release"`、发布物信息）、`archive.read`（这个版本的文件清单）、`archive.file`（读其中一个文件）。
  插件页面因此可以同一份代码两用：在项目里读档案，在发布物里读快照。

## 8. 回流：反馈由读者自己送出

- 读者在发布物页面写一段反馈：存进 `Release.feedback`，在项目群聊里以事件公开（"读者 x 对「…」的反馈：…"）。
  由项目里的人决定要不要 @ 成员去做，网络不自动派活。
- 只有读者主动写的文字回流；订阅者 Agent 的对话、问题不回流（GPTs 也只给作者"用户主动给的反馈"）。
- 参照 Manus"数字同事"的反馈接口（research: manus#self-improving、research: project-publishing#feedback-loop）。

## 9. 新不变量（第 23 条）

> **发布只经主持人，交付件只流向读者**：项目的内容离开项目只经发布（主持人的操作，或主持人对一份交付件开的"跟随档案"），
> 按版本固定、群聊里公开记录；技能包可以给所有人，交付件只给点名的读者；订阅只在订阅者自己的 Agent 上、
> 这一轮输出能到达的人都看得到这份发布物时生效，用订阅者自己的授权；读者只读，反馈只有读者自己写的文字。

测试覆盖：非主持人不能发布；看不到的发布物 404；技能包 / 交付件挂载与"能到达的人"的判断；跟随档案切版本；撤回后不生效；
发布物帧令牌与只读桥；反馈只进群聊事件。

## 10. 节律面（后续分期，不在第一期）

- **职责清单**：把插件触发提成项目页上统一可见的"职责"（触发 + 技能 + 负责成员），定时轮次的身份照不变量 21 处理。
  参照 GitHub Agentic Workflows "默认只读，写出去走安全输出"（research: project-publishing#continuous-ai）、
  research: always-on-personal-agents#matrix-takeaways。
- **请人定**：`ask_decision`，Human on the loop 的主界面（research: manus#takeaway-ask-decision）。
- **整理回合**：项目级定期整理只出提议（纲要修改、带引文校验的候选决定），主持人采纳
  （research: project-publishing#sleep-time、research: hy-memory#for-candidate-decisions）。
- **目录与信誉**：发布物目录的排序信号取网络事实（订阅数、版本新鲜度、反馈），不取发布方自述（research: reason-network#directory）。

## 11. 分期

1. **第一期（已实现）**：`Release`、发布 / 新版本 / 跟随 / 撤回、读者与可见性、订阅（技能包进技能层、交付件只读挂载、能到达的人的判断）、
   发布物页面、插件视图（快照 + 只读桥 + 清单 `release_views`）、反馈、不变量 23 与测试；
   交付看板示例（`examples/delivery-dashboard/`）作为验证场景。
2. 职责清单、`ask_decision`。
3. 整理回合、发布物目录的信号与排序。
4. 对外（A2A）只在网络边界，外部请求不获得内部授权（research: acp-vs-a2a#external-task-authority）。

## 12. 不做

- 项目的公共 Agent 对外服务（见第 6 节）。
- 发布物带发布方的凭证或连接器授权。
- 按发布方自述排序的目录。
- 读者写回发布物、发布物之间互相引用（第一期）。

## 13. 交付看板：三个面怎么落

| 面 | 交付看板 |
| --- | --- |
| 作业面 | 一个交付项目装上"交付看板"插件（表 `dashboards`、看板页面、技能 `delivery-dashboard`）；一线人员授权部署方目录里的"交付数据"连接器（带查数技能），在群聊里 @ 助理做看板；助理查数、生成看板 JSON、`publish_file`、`plugin_write` 登记，`deliver_request` 交付，人验收 |
| 发布面 | 主持人把技能发布成**技能包**（public）：所有交付项目的一线都能订阅；把看板文件 + 插件视图发布成**交付件**，读者是区域负责人，开"跟随档案" |
| 节律面 | 插件的定时触发每天 @ 助理刷新看板文件；跟随档案自动切新版本；负责人在发布物页面写反馈，回流到群聊 |

数据权限跟着人走：每个人的助理用自己授权的连接器查数；区域负责人看到的只是发布出来的快照。
