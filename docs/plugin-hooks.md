# 插件挂点：让插件影响项目的上下文与协作（设计）

> 状态：第一、二期已实现，带示例「项目 Wiki」（`examples/plugins/llm-wiki/`、`examples/llm-wiki/`）；实现与草案不同的地方见第 12 节。
> 当前行为见 [reference/plugins.md](reference/plugins.md)“插件服务与挂点”，不变量 24 的全文见 [reference/invariants.md](reference/invariants.md)。现有插件（表、沙箱页面、技能、触发）见 [project-plugins.md](project-plugins.md) 与 [reference/plugins.md](reference/plugins.md)，
> 不变量见 [reference/invariants.md](reference/invariants.md)。第一个验证场景是 LLM Wiki（第 9 节）。
> 调研：research: deepseek-harness#plugin-model、research: llm-wiki#pattern、research: llm-wiki#adopt。

## 1. 要解决什么

现在的插件是**业务扩展**：表、页面、技能、触发，只能往群聊里 @ 成员，碰不到 Agent 每一轮看到什么、能用什么。
很多值得装进项目的东西恰恰要碰这一层：

- **记忆**：运行之后提炼，运行之前召回。
- **LLM Wiki**：项目资料编译成一份由 Agent 维护的 wiki，提问时先查 wiki（research: llm-wiki#pattern）。
- **协作方式**：Raft 式的被动投递与认领、Multica 式的任务看板与指派。现在这些是"别家产品的过程"，以后应该是可以选装的插件。

方向（2026-10-30 定）：

1. 插件放大成**项目能力的扩展单位**，记忆、Wiki、协作方式都能是插件；Matrix 本体只留最小原语与安全边界。
2. 插件**跑在 Matrix 上，和 harness 解耦**：dsh、Claude Code、Codex，云侧和端侧，装了插件的项目里效果一样。
3. 对外交付只有一种东西，叫**蓝图**（一个配好的项目：纲要、岗位与 Agent、技能、连接器槽位、装了哪些插件、人在哪里介入）；
   插件是蓝图的部件。蓝图另写设计，本文只管插件。

参照 dsh：插件 = 声明的挂点 + 一段代码（research: deepseek-harness#plugin-model）。
不同的是 dsh 的挂点在 harness 进程里，能力绑死这个 harness；Matrix 的挂点定在**网络层**。网络本来就负责组装每一轮的提示词、
挂载工作目录、提供网络工具、处理交付与验收，这些地方就是挂点。

## 2. 原则

1. **挂点在网络，不在 harness。** 插件只通过网络影响一轮运行：往提示词里加一段、往工作目录里挂一份只读材料、给 Agent 一组工具、
   在一轮结束后收到结果。所有运行时拿到的都是网络组装好的东西。
2. **插件改策略，不改安全。** 不变量分两类（第 7 节）。权限、身份、可见性、验收这一类插件动不了；唤醒、上下文里放什么、任务怎么流转这一类可以交给插件。
3. **声明优先，第三方服务不改代码就能接。** 挂点的动作是清单里声明的（挂哪个文件、调哪个工具、参数怎么填），
   插件服务只要是个标准 MCP 服务，不需要 Matrix 专用的 SDK。这样业界现成的 MCP 服务（例如 LLM Wiki 引擎）可以直接包成插件。
4. **模型调用留在 Agent 的运行里。** 插件服务不调模型，只做存储、检索、确定性检查。需要模型的工作（提炼、编译、综合）
   由挂点交给项目里的一个成员 Agent 去做，用它所有者的模型、记在它所有者的用量里，走原来的 @、委托与验收。
   这和 LLM Wiki 引擎"No LLM inside"的分工一致（research: llm-wiki#geronimo）。
5. **插件数据属于项目档案。** 和现有插件记录一样（不变量 17）：可见范围是项目参与者，写入留痕，蓝图发布时不带数据。

## 3. 插件包：多两个部件

在现有的 `plugin.json` 上加 `service`、`hooks`，`skills/`、`page/`、`tables`、`triggers` 照旧：

```json
{
  "name": "llm-wiki",
  "version": "0.1.0",
  "title": "项目 Wiki",
  "description": "项目资料编译成一份由 Agent 维护的 wiki；提问先查 wiki",
  "service": {
    "command": "llm-wiki",
    "args": ["serve", "--space", "{data}"],
    "transport": "stdio",
    "network": false,
    "tools": {
      "wiki_search": "read", "wiki_content_read": "read", "wiki_list": "read", "wiki_lint": "read",
      "wiki_content_write": "write", "wiki_content_new": "write", "wiki_ingest": "write", "wiki_content_commit": "write"
    }
  },
  "hooks": {
    "mount":   [{"path": "wiki", "as": "wiki"}],
    "context": [{"file": "wiki/index.md", "budget": 3000, "label": "项目 Wiki 目录"}],
    "after":   [{"on": "request.accepted", "notify": {"to": "@{maintainer}", "text": "「{title}」已验收，按 wiki-ingest 把结论写回 wiki"}}],
    "settings": {"maintainer": {"type": "agent", "title": "Wiki 维护者"}}
  },
  "skills": ["wiki-ingest", "wiki-query", "wiki-lint"],
  "triggers": [{"key": "weekly-lint", "type": "schedule", "cron": "0 9 * * 1",
                "notify": {"to": "@{maintainer}", "text": "按 wiki-lint 体检一遍"}}],
  "tabs": [{"key": "wiki", "label": "Wiki", "entry": "page/index.html"}]
}
```

- `{data}` 是这个安装的数据目录，`{maintainer}` 等是安装时主持人填的设置（`hooks.settings`，类型沿用字段词表，`agent` 是项目成员）。
- 清单里没有 `service` 的插件和现在完全一样。

## 4. 插件服务：网络托管

| 项 | 规则 |
| --- | --- |
| 来源 | **只来自部署方目录**（`plugins.json`）。主持人上传的 zip 不能带 `service`：服务端执行的代码要部署方审核过才装。`command` 解析为目录里登记的程序，不经 shell |
| 进程 | 一个安装（项目 × 插件）一个进程，隔离最简单。按需启动，空闲一段时间停掉；崩溃按次数上限重启，超限暂停并告诉主持人 |
| 数据 | `projects/<id>/plugins/<name>/data/`，进程工作目录就在这里。卸载时问主持人留不留数据 |
| 传输 | 第一期只做 stdio MCP；网络是 MCP 客户端（现有 `mcp_client.py` 只有 Streamable HTTP，要补 stdio） |
| 环境 | 不继承服务进程的环境；只给清单声明、部署方配置的变量。插件服务**不拿任何人的凭证**，要连外部服务走连接器（不变量 19） |
| 联网 | `network: false` 是默认值，也是声明。第一期靠部署方审核兜底；强制隔离（无网络命名空间、只读挂载）和 [security.md](reference/security.md) 里"沙箱只管写、不管读"的已知风险一起做 |
| 用量 | 每次调用记一行插件用量（插件、项目、工具、耗时、成败，不记内容），管理后台可看 |

## 5. 挂点

| 挂点 | 什么时候 | 动作 | 落在哪里（现有代码） |
| --- | --- | --- | --- |
| `tools` | 每一轮 | 服务的工具按清单白名单给项目成员 Agent，名字 `plugin__<插件>__<工具>`；`write` 类要插件申请了 `agent_tools` 的写能力，且按第 6 节过信息流 | 和网络工具同一条通道（见下） |
| `mount` | 每一轮运行前 | 数据目录里的一个子目录**只读**挂进工作目录 `plugins/<插件>/<as>/`，运行后丢弃 | `Network._run` 里 `_mount_project_files` 旁边；端侧随 `run.request` 的材料下发 |
| `context` | 每一轮运行前 | 往提示词里加一段，标明来源插件。`file`：数据目录里一个文件，截到 `budget`，变了才重推（同 `abilities_seen`）；`tool`：调一个 `read` 类工具，参数模板可用 `{mission}`（任务目标）、`{latest}`（这一轮 @ 它的那条消息），结果截到预算，每轮都调 | `_seat_round` / `_task_round` 拼档案推送的地方；全部插件共用一个总预算 |
| `after` | 事件之后 | `notify`：像触发一样往群聊发消息 @ 一个成员，代表启用它的人；`tool`：调一个工具，把事件内容（标题、交付说明、产物文件名）作为参数 | 交付、验收、回复落定的地方（`_deliver`、验收处理、`_seat_round` 末尾） |
| `page` / `tables` / `triggers` | 照旧 | 照旧 | [project-plugins.md](project-plugins.md) |
| `route`（预留） | 一条消息、一个任务要分给谁时 | 给出建议：唤醒谁、谁来认领 | 第三期，见第 8 节 |

`after` 的事件第一期只开这几个：`request.accepted`（人验收了一个用户任务）、`task.accepted`（委托方验收了一个委托）、`file.published`（档案里多了文件）。
**只接验收过的结果**，不接每一条回复：写进 Wiki、记忆的应该是确认过的东西。验收的人就是那道质量关。

### 和 harness 解耦：工具走网络工具的通道

云端连接器现在只给云侧运行（`_invoke` 里 `if cloud and not agent.device_runtime`），端侧拿不到。网络工具（`mcp__matrix_net__*`）
则在三种 harness、云侧和端侧都通：云侧经 `mcp_server.py`，端侧经连接器的本机桥转发 `/internal/tools`。

所以插件工具**挂在网络工具这条通道上**：`mcp_server.py` 的工具列表不再只是静态的 `TOOLS`，而是加上"这一轮这个 Agent 能用的插件工具"
（向 `/internal/tools` 取，凭执行上下文令牌）；调用回到网络，网络转给插件服务。身份、权限、留痕都在网络这一侧（不变量 10、11），
插件服务看到的只是网络转过去的参数。`mount` 和 `context` 本来就在网络这一侧完成，不涉及 harness。

## 6. 可见性与信息流

插件服务是项目档案的一部分，读写都要放在现有的可见性和信息流规则里，不能交给插件或模型自觉。现成的 LLM Wiki 实现都是单人的，
这一层是 Matrix 要补上的（research: llm-wiki#matrix-takeaways）。

- **只在本项目里生效。** 挂点只对这个项目里的运行生效：群聊席位、项目会话、这个项目的委托。助理对话（跨项目）、私聊、别的项目都拿不到。
- **读：** 插件数据的可见范围 = 项目参与者。注入和挂载进项目里的运行，不会越过这个范围。
- **写：** 一轮运行通过 `write` 类工具写插件数据，等于这一轮的输出到达了全体项目参与者。网络按不变量 22 判断：
  这一轮的来源里有当时不在场的人的场合时（例如项目会话里读了私有目录的笔记），这次写入先给 Agent 的所有者确认，确认后才转给服务。
  复用现有的 `_over` / `_hold`。
- **私有目录**：插件读不到，也不能被注入（不变量 8）。
- **`after.tool` 带出去的内容**：只是已经在项目里公开的东西（验收过的交付说明、档案里的文件）。

## 7. 不变量：分两类，改一条、加一条

**安全类（插件不能改）**：1 权限只收窄、2 只委托项目成员、3 约束累加、4 防环限深、5 只有直接委托方能验收、
8 私有目录归 Agent 自己、10 身份由网络认定、11 越权留痕、13 归属与可见性、14 外溢由所有者批准、16 不跨信任域、
19 连接器凭证、21 助理对话、22 信息只流向在场的人、23 发布只经主持人。

**策略类（插件可以影响，但只经声明的挂点）**：上下文里推什么、推多少（17 的推送部分）；唤醒规则（现在是只有人的 @ 唤醒，
深度与次数上限属于 4，不能动）；任务怎么分配（6"协作只走网络"不变，分配建议可以来自插件）。

**改第 20 条**：限定为"插件**页面**代码只在浏览器沙箱里"，原文其余不变。

**新第 24 条（草案）**：

> **插件服务只来自部署方目录，只经网络、按挂点作用**：服务端执行的插件代码只能来自部署方目录，不来自上传；每个安装一个进程，
> 工作目录是这个安装的数据目录，不拿任何人的凭证。插件只经声明的挂点影响运行（工具、只读挂载、提示词里标明来源的一段、
> 事件后的通知或调用），只对本项目里的运行生效；工具调用的身份、权限与留痕由网络认定，写入按不变量 22 判断；
> 插件不能改变安全类不变量规定的任何结果。

测试要覆盖：上传的包带 `service` 被拒；服务进程的环境里没有凭证与网络密钥；插件工具只出现在本项目的运行里，助理对话、私聊没有；
没申请写能力的 `write` 工具被拒；来源越界的写入先确认；`context` 注入有预算、标来源；`after` 只在验收后触发，代表启用它的人；
停用或卸载后挂点全部失效。

## 8. 协作类插件（第三期，先定边界）

Raft、Multica 这类东西做成插件，要的是 `route` 挂点：一条消息该唤醒谁，一个任务谁来认领。边界先定下来：

- 插件只能**建议**唤醒或认领，最终由网络执行，并照样受深度、次数、成环检查（不变量 4）。
  Raft 出过同一个 Agent 被自动唤醒 181 次的事故，结构性上限不能交给插件（research: raft#crash-loop-breaker、research: raft#inbox-wake）。
- 认领只能在项目成员之间（参照 research: raft#tasks-claim），委托仍要经 `delegate`，验收仍归直接委托方（不变量 2、5、6）。
- "一个 Agent 一个会话跨所有频道"直接违反默认隔离（不变量 7），做成插件也不允许。

## 9. 第一个验证场景：LLM Wiki

选型见 research: llm-wiki#adopt：**工作流用 Astro-Han 的技能（MIT），引擎用 geronimo 的 llm-wiki（MIT / Apache-2.0），Matrix 只写接线。**
这个插件把第 3–6 节的部件都用上：

| 部件 | 内容 | 验证什么 |
| --- | --- | --- |
| `service` | llm-wiki 程序，一个项目一个进程，数据在 `projects/<id>/plugins/llm-wiki/data/` | 部署方托管的第三方 MCP 服务，不改它的代码 |
| `tools` | 搜索、读取、写入、lint，读写分级 | 插件工具在 dsh、Claude Code、Codex 上都能用 |
| `skills` | Astro-Han 的 ingest / query / lint，改写成调用引擎工具，保留目录约定、grounding 规则和 log 格式 | 插件带第三方技能 |
| `mount` | `wiki/` 只读挂进工作目录，query 可以直接读文件 | 只读挂载，云侧拷贝，端侧下发 |
| `context` | 每轮推 `wiki/index.md`（截到预算） | 插件影响上下文，这是本期最关键的新能力 |
| `after` | 用户任务验收后 @ Wiki 维护者写回 | 运行结果回流，只收验收过的 |
| `triggers` | 每周 lint | 复用现有触发 |
| `page` | Wiki 标签：浏览、逐页历史、lint 报告 | 复用现有页面插件 |
| 信息流 | 项目会话里用了私有笔记的写入先确认 | 第 6 节 |

**退路**：geronimo 的引擎不合适时（项目隔离、中文检索、维护活跃度，见 research: llm-wiki#to-verify），用 Python 标准库写一个薄引擎，
放在 `examples/plugins/llm-wiki/` 里，工作流照用 Astro-Han。插件清单不变，正好也证明了"换掉服务不影响声明"。

验收：一个演示项目装上插件，`setup_demo.py` 搭好资料，脚本运行时走通"放入资料 → ingest → 提问时带上 wiki 目录 → 带引用作答 →
验收后写回 → 每周 lint"；再用真实 dsh 跑一遍，并在一台端侧机器上用 Claude Code 跑同一个场景。

## 10. 分期

1. **插件服务与工具**：清单的 `service`、部署方目录限制、进程托管（stdio MCP 客户端、按需启停、重启上限）、插件工具经网络工具通道到三种 harness、
   读写分级与留痕、插件用量、不变量 20 修改与 24、测试。
2. **上下文挂点**：`mount`、`context`（`file`、`tool`）、预算与来源标注、写入的信息流判断；LLM Wiki 示例插件走通第 9 节。
3. **事件挂点**：`after`（`notify`、`tool`），设置项（`hooks.settings`）；记忆插件作为第二个验证场景（隐式提炼，可见性更难）。
4. **协作挂点**：`route`，第 8 节的边界；一个 Raft 式或 Multica 式的协作插件。

每一期同步更新 [about.html](../matrix/static/about.html) 的架构图、[plugin-authoring.md](plugin-authoring.md) 与 `docs/reference/plugins.md`。

## 11. 不做

- 插件代码进 harness 进程（dsh 式的进程内插件）：能力会绑死一个 harness，端侧的 Claude Code、Codex 用不上。
- 插件服务调模型：模型调用的身份、计费、凭证都归 Agent 的所有者，插件要用模型就把活交给成员 Agent。
- 主持人上传带服务端代码的插件。
- 插件改写或删掉网络组装的提示词其他部分（只能追加标明来源的一段），插件之间互相调用。
- 跨项目共享的插件数据：要跨项目就经蓝图或发布物，数据不随蓝图走。

## 12. 实现时改动的决定（2026-10-30）

- **工具不按插件生成名字，统一成两个网络工具** `plugin_tools` / `plugin_call`（草案是 `plugin__<插件>__<工具>`）：`mcp_server.py` 的工具表保持静态，
  三种 harness 不用为每个项目动态改工具列表；参数仍走 JSON 字符串（避开数组参数的问题）。白名单与读写分级照旧在网络这一侧检查。
- **越界的写入直接拒绝，不挂起等确认**：和其他会让别人看到内容的网络工具一致（不变量 22 的现有做法），模型换个说法写进回复，由回复的拦截确认处理。
- **`after.notify.to` 支持 `{agent}`**（接任务的 Agent），`hooks.settings` 没做：一个蓝图里装的插件不该写死成员名字，`{agent}` 已经够第一个场景用。
- **刚装上时先拉起服务**：数据目录还空着时，挂点先启动服务让它初始化，第一轮就有目录可推。
- **LLM Wiki 的引擎用自写的 Python 薄引擎**（第 9 节的退路）：只用标准库，三端 CI 都能跑，测试确定；工作流按 Astro-Han 的技能改写
  （research: llm-wiki#astro-han）。换成 geronimo 的 Rust 引擎只需改清单里的 `service`，挂点与技能不变。
- **页面的引用**：wiki 里 `wiki/…`、`raw/…` 开头的链接按数据根目录解析（Agent 在对话里引用页面就这么写，写回时原样带进页面）。
