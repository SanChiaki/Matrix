# 管理后台

> 入口见 [AGENTS.md](../../AGENTS.md)。代码：`matrix/usage.py`（台账与统计，纯函数）、`engine.py`「管理后台」一节、
> `server.py` 的 `/admin` 与 `/api/admin/usage`、`static/admin.*`。测试：`tests/test_admin.py`。

部署方看整个网络的使用情况：多少人在用、跑了多少轮、花了多少 token、哪里在失败；并配置平台云侧 Agent 能用的模型。

## 谁能看

- 部署方用 `--admins` / `MATRIX_ADMINS`（邮箱，逗号分隔，不区分大小写）指定管理员；没指定时是**最早注册的那个人**。
- 不是管理员的人：`/api/admin/*` 按不存在处理（404，同不变量 13），侧栏里没有入口；`/admin` 页面本身是静态的，打开只看到“没有权限”。
- `/api/me` 带 `admin`，前端据此显示“管理后台”入口。

## 只看用量，不看内容

管理员是部署方的角色，不是项目参与者。统计里只有计数、类别（场合、运行位置、模型、错误类别）与账号邮箱，
**不出现项目名、Agent 名、消息正文、提示词、回复、工具参数**。`test_admin_dashboard` 会在统计或台账里出现这些内容时变红。
新增统计项时守住这一条：要按项目、按 Agent 细分，只给 id 级别的计数，不带名字。

## 模型目录（“模型”标签页）

管理员在后台编辑的就是云侧预置模型目录（`model_catalog.py`，默认 `<data>/models.json`，`--models` / `MATRIX_MODELS` 可改），
和手改文件是同一份：用户创建、编辑云侧 Agent 时只能从里面选，不选用目录默认，再没有用运行时默认；端侧 Agent 不受约束。

- 添加第一个模型就启用目录（建出文件）。启用前自己填了模型的 Agent：和目录里某个模型的 provider / model / 推理强度完全一样就算选了它，
  否则这一轮起改用默认。页面列出“在用、但不在目录里的模型”和用它的 Agent 数，可以一键加进目录。
- 删除模型：用它的 Agent 下一轮改用默认（页面先提示有几个在用）；删的是默认模型时默认改回运行时默认。标识保存后不能改。
- 整份保存：先用 `parse_catalog` 校验，不合法不落盘；带着读到时的版本（文件内容的 sha256），期间文件被别处改过就 409，不覆盖。
  写临时文件再替换。保存后发 `models.updated`，打开着的页面重新拉 `/api/models`。
- 后台不能关掉目录（删掉 `models.json` 才回到自由填写）；文件写坏时页面显示原因，保存会整份覆盖它。
- 接口：`GET` / `PUT /api/admin/models`。只给每个模型在用的云侧 Agent 数，不给 Agent 名。
- 模型的供应商有两种：
  - **平台配的供应商**（页面下方“供应商”）：名称、协议（`openai-completions` / `openai-responses` / `anthropic-messages`）、地址、密钥，
    字段照 dsh 的 `llm-pi-ai`（research: deepseek-harness#model-providers）。用它的模型还可以填上下文长度、单次最多输出、能不能看图片；
    推理强度只能是 dsh 的名字（`off` … `max`），原样交给供应商。
  - **dsh 里已配置的供应商**（如 `deepseek-official`）：只写名字，provider / model 要和服务器上 dsh 的配置一致，凭据在 dsh 里配
    （[deploy/README.md](../../deploy/README.md)）。
- 供应商的密钥：
  - 只写不读：`PUT` 里的 `providers[].api_key` 有值才换，空着沿用；返回、页面只有 `has_key`。删掉供应商时密钥一起删；还有模型在用的供应商页面不让删。
  - 存在保管库（`vault/model-provider-<id>.bin`，和连接器凭证同一把保管密钥），不进 `models.json`、`state.json`、事件、日志、台账。
  - 云侧 Agent 用到它的那一轮（`Network._llm_provider`）才解开，只加进那个 dsh 进程的环境变量 `MATRIX_LLM_<ID>_API_KEY`；
    覆盖层（在工作目录里，Agent 读得到）只写 `apiKeyEnv` 变量名与这一个供应商（dsh 对 `llm-pi-ai` 的配置整段替换，写别的会盖掉
    dsh 自己配的）。端侧运行时从不下发。
  - 变量名里有 `KEY`：dsh 按名字把这类变量从 Agent 的命令环境里滤掉。这是 dsh 的行为，`matrix-dsh-check` 的 `key-hidden`
    用假服务核对（printenv、`/proc/*/environ`、`ps eww`），升级 dsh 时变红就不能上线。
  - 没设密钥时 dsh 不发请求，这一轮报 `MISSING_CREDENTIAL`；页面在供应商和用它的模型上标“未设置”。

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

- 只追加，不写进 `state.json`；写失败只在服务日志里记一行（事件会推给所有人，错误里有服务器路径），不影响这一轮。坏行读的时候跳过。
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
