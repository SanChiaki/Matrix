# 管理后台

> 入口见 [AGENTS.md](../../AGENTS.md)。代码：`matrix/usage.py`（台账与统计，纯函数）、`engine.py`「管理后台」一节、
> `server.py` 的 `/admin` 与 `/api/admin/usage`、`static/admin.*`。测试：`tests/test_admin.py`。

部署方看整个网络的使用情况：多少人在用、跑了多少轮、花了多少 token、哪里在失败。

## 谁能看

- 部署方用 `--admins` / `MATRIX_ADMINS`（邮箱，逗号分隔，不区分大小写）指定管理员；没指定时是**最早注册的那个人**。
- 不是管理员的人：`/api/admin/*` 按不存在处理（404，同不变量 13），侧栏里没有入口；`/admin` 页面本身是静态的，打开只看到“没有权限”。
- `/api/me` 带 `admin`，前端据此显示“管理后台”入口。

## 只看用量，不看内容

管理员是部署方的角色，不是项目参与者。统计里只有计数、类别（场合、运行位置、模型、错误类别）与账号邮箱，
**不出现项目名、Agent 名、消息正文、提示词、回复、工具参数**。`test_admin_dashboard` 会在统计或台账里出现这些内容时变红。
新增统计项时守住这一条：要按项目、按 Agent 细分，只给 id 级别的计数，不带名字。

## 用量台账

每次调用运行时（`Network._invoke`，自动续接的那一次也算一次），追加一行到 `<data>/usage.jsonl`：

| 字段 | 含义 |
| --- | --- |
| `ts` / `ms` | 开始时间、耗时 |
| `user` / `agent` / `project` | Agent 的所有者、Agent id、所在项目 id |
| `venue` | `project`（群聊）· `session`（项目会话）· `dm`（私聊）· `home`（助理对话）· `task`（委托） |
| `where` / `runtime` | `cloud` + 运行时名，或 `device` + harness |
| `model` | `provider / model`；`null` = 运行时默认 |
| `ok` / `error` | 成没成；失败的错误类别（`runtime.ERROR_CLASSES`），端侧不在线记 `device_offline` |
| `tools` / `usage` | 工具调用次数；运行时报告的原始 token 字段 |

- 只追加，不写进 `state.json`；写失败只发 `usage.error` 事件，不影响这一轮。坏行读的时候跳过。
- 各运行时报告 token 的字段名不同，统计时由 `usage.tokens` 归一成输入 / 输出 / 缓存命中 / 合计；原始字段留在台账里。
- 这个功能上线之前的运行没有记录：统计带 `recording_since`，页面上提示从哪天开始记的。

## 统计（`GET /api/admin/usage?days=7|30|90`）

按服务器当地时间分天。返回：

- `totals`：这段时间的轮次、失败、token、人发的消息、用户任务、委托、工具调用、连接器调用、活跃用户；
  以及现在的用户、项目（共享）、Agent（端侧、正在运行）、设备（在线）、我的连接器、项目插件、待审核提议。
- `daily`：每天的轮次、失败、token、消息、用户任务、活跃用户。
- `users`：每个账号的注册时间、最近活跃（最近一次发消息、Agent 运行或登录）、项目数、Agent 数、消息、用户任务、连接器调用、轮次、token。
- `models` / `runtimes` / `venues` / `errors`：按模型、运行位置、场合、失败原因分组。

消息、用户任务来自 `state.json`；连接器调用来自各连接的 `calls.jsonl`；轮次与 token 来自用量台账。每次请求现读现算，
数据量大到慢的时候再加按天的汇总缓存。
