# 项目插件（设计）

> 状态：第一版已实现，当前行为见 [reference/plugins.md](reference/plugins.md)，怎么写插件见 [plugin-authoring.md](plugin-authoring.md)；与本文不同或还没做的地方在各条里标了“（第一版：…）”。
> 项目模型见 [projects.md](projects.md)，不变量见 [reference/invariants.md](reference/invariants.md)。

## 1. 要解决什么

项目现在只有一套固定的页面（资产、资源引用、技能、需要的连接器）和一套固定的协作方式（群聊里 @、委托、看板）。
不同业务要的东西不一样：销售要一个线索漏斗，招聘要候选人看板，内容团队要选题排期，研发要一个“需求 → 方案 → 实现 → 验收”的推进视图；
外部系统（表单、工单、电商、CRM）的事件也要能进到项目里，交给合适的成员。

**插件**是给项目加上一个业务场景的单位：它带来

1. **页面**：项目页上的一个或几个标签，内容由插件自己的前端代码画；
2. **数据**：插件自己的结构化记录（表），归项目所有，是项目档案的一部分；
3. **打法**：这个场景怎么做事（阶段、进入 / 完成的标准、通常交给谁），以技能的形式给成员 Agent；
4. **触发**：外部事件、定时、记录变化，变成群聊里 @ 某个成员的一条消息。

已定的取舍：

- 页面支持**自定义代码**（HTML / JS），一开始就支持；代码只在浏览器沙箱里跑，不在服务器、不在运行时里跑。
- 来源两种：部署方维护的插件目录（同 `connectors.json`），主持人上传插件包（同项目技能的上传）。
- Workflow 是**打法 + 触发**，不是硬编排：插件不能直接派委托、不能替 Agent 决定下一步。
  纲领写明“我们不构建一个固定流程编排器”，网络只负责把事情送到人和 Agent 面前，谁委托谁仍由 Agent 自己判断（不变量 2、5、6 不变）。

参照 Kylon 的 Apps 与 Automations（research: kylon#apps、research: kylon#automations-followups）：
App 属于一个 Room、数据由平台托管、发布要人审核；Automation 属于 Room，人创建的保留这个人的委托身份。
不同的是 Matrix 的插件不托管后端代码，业务逻辑要么在浏览器里（页面），要么交给 Agent（打法），网络只做存储、权限与路由。

## 2. 插件包

一个文件夹（上传时打成 zip，规则同技能包：可以多一层顶层目录，`__MACOSX`、`.DS_Store` 忽略，点开头或越界的路径拒绝）：

```
sales-pipeline/
  plugin.json          # 清单
  page/                # 页面代码：index.html + 任意静态资源
  skills/<名字>/       # 打法：标准技能包，装上后成为项目技能（来源标“插件”）
```

`plugin.json`：

```json
{
  "name": "sales-pipeline",
  "version": "1.0.0",
  "title": "销售漏斗",
  "description": "线索从获取到成交的推进",
  "tabs": [{"key": "funnel", "label": "销售漏斗", "entry": "page/index.html"}],
  "tables": {
    "leads": {
      "title": "线索",
      "fields": [
        {"key": "company", "type": "text", "required": true},
        {"key": "stage", "type": "select", "options": ["线索", "沟通", "报价", "成交", "流失"]},
        {"key": "owner_agent", "type": "agent"},
        {"key": "note", "type": "longtext"}
      ]
    }
  },
  "page_permissions": ["records.read", "records.write", "archive.read", "chat.post"],
  "agent_tools": ["records.read", "records.write"],
  "triggers": [
    {"key": "new-lead", "type": "webhook", "table": "leads",
     "notify": {"to": "@销售助理", "text": "新线索：{company}，请按 sales-intake 跟进"}},
    {"key": "weekly", "type": "schedule", "cron": "0 9 * * 1",
     "notify": {"to": "@销售助理", "text": "整理本周漏斗变化，发到群聊"}}
  ]
}
```

- 名字规则同技能（小写字母、数字、`-`，≤64）；包大小、文件数、表数、字段数、每表记录数都有上限（`models.PLUGIN_*`）。
- 字段类型是一张小词表：`text` / `longtext` / `number` / `date` / `select` / `bool` / `agent`（项目成员）/ `file`（项目文件名）。
  写入时网络按清单校验，页面和 Agent 都不能写清单以外的字段。
- `page_permissions`、`agent_tools` 是插件**申请**的能力，安装时展示给主持人；实际能用的是申请的 ∩ 当时用户（或 Agent）本身有的（不变量 1 的同一个口径）。

## 3. 安装与版本

- 部署方目录：`plugins.json`（默认 `<data>/plugins.json`，`--plugins` / `AGENTNET_PLUGINS`），列出插件包所在目录，按修改时间热加载，写坏了沿用上一份。
- 主持人在项目页“插件”标签里从目录安装，或上传插件包。安装时把包**拷进** `projects/<id>/plugins/<名字>/`，固定版本；
  目录里的新版本只提示、由主持人点升级，不自动换（人看过的代码才在大家的浏览器里跑）。
- 升级时清单里的表结构变化：新增字段 / 选项直接生效；删字段、改类型要主持人确认，旧数据保留在 `log.jsonl` 里可以找回。
- 主持人可以先**停用**而不卸载：插件对页面、Agent 与触发一律不可用，包、记录与设置都留着，重新启用即恢复。
- 卸载保留数据（`plugin-data/<名字>/`），可以重新装回。（第一版：一直保留，没有 30 天清理。）
- 安装、升级、启停、卸载都写进 `log.jsonl` 并在群聊里记一条事件；联合项目里参与者能看到装了哪些插件、申请了哪些能力、谁装的。
- Agent 可以用 `propose_plugin` 提议安装目录里的插件（不能提议上传代码），主持人批准后生效，同 `propose_skill`。（第一版：没做。）

## 4. 页面：沙箱里的自定义代码

插件标签在项目页里是一个 iframe：

- `sandbox="allow-scripts allow-forms"`，**不给** `allow-same-origin`、`allow-popups`、`allow-top-navigation`：
  （`allow-forms` 只为让表单的 `submit` 事件照常触发——没有它，浏览器连事件都不发，插件里的表单全都失灵；真正的提交被 CSP `form-action 'none'` 挡下）
  页面是不透明来源，读不到父页面、拿不到登录 cookie（cookie 是 `SameSite=Lax`，不透明来源发出的请求不带），不能弹窗、不能跳走主页面。
- 插件文件从 `/plugin-frame/<短期令牌>/<路径>` 下发，令牌绑定 用户 + 项目 + 插件 + 版本、几分钟有效，只能取这个插件的静态文件，拿不到任何数据。
  响应头带 `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'none'; script-src 'self' 'unsafe-inline';
  style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'none'; form-action 'none'; frame-ancestors 'self'`：
  直接打开这个地址也在沙箱里；不能 `fetch` 任何地方，也不能从外部加载脚本、图片。外部服务要走连接器或触发，不从浏览器直连。
- 数据只经 **桥**：父页面里的一小段代码监听 iframe 的 `postMessage`，按插件申请的能力把请求转成普通的 `/api/...` 调用，
  以**正在看页面的人**的身份执行，回包同样经 `postMessage` 送回。父页面校验 `event.source` 是这个 iframe，不按消息内容认身份。
- 桥提供的能力（第一期）：

| 能力 | 做什么 | 范围 |
| --- | --- | --- |
| `records.read` / `records.write` | 读写插件自己的表 | 只有这个插件的表；写入按清单校验 |
| `archive.read` | 读纲要、有效决定、文件列表与内容、资源引用 | 项目档案（本来就对所有人可见） |
| `members.read` | 成员 Agent 的名片 | 同项目里的名片口径 |
| `chat.read` | 群聊的公开消息 | 只有对所有参与者可见的消息，不含 `audience` / `private_to` 限定的内容 |
| `chat.post` | 以看的人的身份在群聊里发一条消息（可以 @） | **每次**在父页面弹确认卡，显示原文，人点“发送”才发 |
| `requests.read` | 看板上的用户任务 | 按看的人的可见性 |
| `ui.*` | 设置标签角标、打开项目文件预览、提示 | 只影响父页面里这个插件的区域 |

- **不给**的：私聊、项目会话、委托详情、别人的连接器与连接状态、审批、Agent 设置、任何账号信息。
  联合项目里插件是主持人装的，代码在参与者的浏览器里跑，所以桥只给“这个项目里所有参与者本来就看得到的东西”，
  外加看的人自己的用户任务；`chat.post` 这类以看的人名义的动作一律逐次确认（参照 Kylon 审批卡“聊天里的回复不代替卡片上的批准”，research: kylon#approval-cards）。
- 已知风险：沙箱挡不住插件页面把**自己**导航到外部地址（`location = "https://…?data"`），等于能把它读到的数据带出去。
  所以桥的读范围压到“全体参与者可见”，插件代码要人安装、按版本固定、来源可见；这个风险写进 [security.md](reference/security.md)。

## 5. 数据：插件记录属于项目档案

- 存在 `projects/<id>/plugin-data/<插件>/<表>.json`，不进 `state.json`（记录可能多）；每条记录 `{id, fields, created_at/by, updated_at/by}`，
  `by` 是人或 Agent，经页面写的另记 `via: <插件>`。
- 写入只经人的操作（页面桥、项目页）或 Agent 的记录工具，变更追加到 `log.jsonl`（不变量 17 的延伸）。
  记录的增删改不写进群聊（太吵），插件可以自己用 `chat.post` 发总结；安装 / 卸载 / 表结构变化写。
- 记录变化 +1 插件自己的 `data_rev`，**不**动 `archive_rev`（不让每改一条记录就重推整份档案）。
- 联合项目里所有参与者都看得到全部记录（同档案）；页面与工具描述里写明“写进去等于对所有参与方公开”。

## 6. Agent 这一侧

- **打法**：插件包里的 `skills/` 装上后合并进项目技能（同名时项目技能 > 插件技能 > 连接器自带 > 我的），
  推送时列出名字与描述，来源标插件。打法写的是阶段、进入 / 完成的标准、通常交给谁、交付格式——是给 Agent 看的做法，不是网络执行的流程。
- **记录工具**：清单里 `agent_tools` 申请了的，成员 Agent 有两个档案类工具（不需要 `delegate`）：
  `plugin_records`（按表、筛选、关键词查，有条数上限）、`plugin_write`（新增或改一条，`fields` 是 JSON 字符串，避开数组参数的问题）。
  新增工具同步改 `mcp_server.TOOLS`、`engine.TOOL_HANDLERS`、`protocol.TOOL_GUIDE`。
- **推送**：档案推送里加一段“这个项目装了的插件”：名字、描述、有哪些表（表名、字段、条数），只在插件集合或表结构变化时重推；记录本身不推，要用就查。

## 7. 触发：外部事件进项目

触发只有一种结果：**往群聊里发一条消息，@ 一个成员**（可选先写一条记录）。之后的事走原来的路：被 @ 的 Agent 自己做或委托、
需要交付的登记用户任务、由人验收。网络不按触发派委托。

| 类型 | 何时触发 | 备注 |
| --- | --- | --- |
| `webhook` | 外部 POST `/hooks/<安装级随机令牌>` | 令牌在“插件 → 触发”里给主持人看、可重置；可选 HMAC 校验（`secret` 只进 `vault/`；第一版：没做，令牌即授权，只存哈希）；请求体按 `table` 映射成一条记录，字段按清单校验，多余的丢弃；限速、限大小 |
| `schedule` | cron（本地时间），最小间隔 10 分钟 | 参照 Kylon（research: kylon#automations-followups） |
| `record` | 某张表的记录新增 / 某字段变成某值 | 只认人的写入和 webhook 写入；Agent 写记录不触发，防止 Agent 之间经插件绕开委托互相唤醒（不变量 6）与成环 |

- 消息的发送者显示为“插件 · 销售漏斗”，**代表启用这个触发的人**（参照 xMatrix Automations“以创建者身份运行”、
  Kylon“人创建的 Automation 保留这个人的委托身份”，research: xmatrix#pages、research: kylon#automations-followups）。
  被 @ 的 Agent 据此登记的用户任务，下发人是这个人，由他验收（不变量 10：下发人仍由网络从送达的消息认定）。
- 只有主持人能启用触发；触发的人退出项目，触发自动停用。连续失败（如目标成员被移出）3 次暂停，主持人恢复。
- 运行中又来的同一触发合并成一条（消息里带“又来了 N 条”），不排长队。（第一版：没做合并，用每个回调令牌每分钟 60 次限速兜底。）
- 联合项目里 @ 别人的 Agent 和主持人自己发消息 @ 它一样：别人的 Agent 仍只代表它的所有者运行，外溢审批仍找它的所有者（不变量 14、16）。

## 8. 新不变量（第 20 条）

> **插件代码只在浏览器沙箱里，只经网络、以看的人的身份读写**：插件页面跑在不透明来源的 iframe 里，拿不到登录凭证、不能直连网络；
> 读写只经父页面的桥，能力 = 插件申请的 ∩ 看的人本身有的，读范围不超过项目里全体参与者可见的内容；以看的人名义的动作逐次确认。
> 插件数据是项目档案的一部分（不变量 17）；触发只往群聊发消息、代表启用它的人，不派委托。网络不在服务器和运行时里执行插件代码。

测试覆盖：沙箱属性与 CSP 头、帧令牌只取静态文件且过期作废、桥按能力与可见性裁剪、`chat.post` 无确认不发、
记录写入按清单校验与留痕、Agent 写记录不触发 `record`、webhook 令牌与限速、触发消息的下发人认定。

## 9. 分期

1. **插件包与页面**：清单校验、上传 / 目录安装 / 卸载、`/plugin-frame` + CSP、桥（`records.*`、`archive.read`、`members.read`、`ui.*`）、
   插件数据存储与 `log.jsonl`、项目页“插件”标签与插件标签页、不变量 20 与测试，带一个示例插件（销售漏斗或“阶段看板”）。
2. **Agent 侧**：插件技能并入项目技能、`plugin_records` / `plugin_write`、档案推送里的插件摘要、`propose_plugin`。
3. **触发**：webhook、schedule、record，触发消息的身份与验收，暂停与恢复。
4. **以人名义的动作**：`chat.post`、`chat.read`、`requests.read`，父页面确认卡。

每一期都同步更新 [about.html](../agentnet/static/about.html) 的架构图（新增插件、桥、触发入口）与 `docs/reference/` 里的说明。

## 10. 不做

- 服务器端插件代码（自定义后端、插件自带 MCP 服务）：等于让上传者在网络里执行任意代码。后端逻辑交给 Agent（打法 + 记录工具）或外部系统（经 webhook / 连接器）。
- 插件直接派委托、按步骤自动推进：违背纲领的方向，也绕开“只有直接委托方能验收”。
- 按记录的访问控制：同 Kylon，“筛选过的视图不是隐私边界”（research: kylon#apps）；要隔离就分项目。
- 插件之间互相调用、读别的插件的表：第一期不做，需要时在清单里显式声明依赖再说。
