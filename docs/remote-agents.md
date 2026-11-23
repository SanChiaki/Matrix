# 外部平台 Agent（设计与第一版实现）

> 状态：**第一版已实现**，不变量 28。需求见 [REQUIREMENTS.md](../REQUIREMENTS.md) #71。接真实平台的步骤见
> [remote-agent-authoring.md](remote-agent-authoring.md)，样板见 `examples/remote-agent/`。公共 Agent 见 [public-agents.md](public-agents.md)。
> 运行时契约与云侧、端侧的差异见 [device-runtimes.md](device-runtimes.md)。调研：research: acp-vs-a2a#external-task-authority。

## 1. 要解决什么

公司内部另有一个“建 Agent 的平台”：人在上面建云端 Agent，给它配技能、连接器，经 HTTP 发起会话。这些 Agent 要接进 Matrix
**当成员**：能被 @、接委托、交付；能用 Matrix 的网络工具（查成员、委托、档案）；办业务时用找它的那个人自己的授权。

它们不跑在 Matrix 里，也不跑在用户电脑上：是**第三种位置**（`placement = "remote"`）。网络对它们和云侧、端侧同等看待，
只是能强制的东西更少。

## 2. 已定的事（2026-11-18）

| # | 决策 |
| --- | --- |
| 1 | 作为**成员**接入，不是作为某个 Agent 的工具 |
| 2 | 平台**支持续接会话**；结果**流式返回**；有**会话中断**接口 |
| 3 | 平台的 MCP 是 **Agent 级配置**（在平台网页上填，没有管理接口），MCP 请求里**带会话 id** |
| 4 | 建会话与发第一条消息是**同一次请求**：MCP 调用可能比流里的会话 id 先到 → 先等再绑 |
| 5 | 平台能读 Agent 的名称与描述 |
| 6 | 调用方的授权用**请求头透传**，平台 Agent 再透传给它的工具 |

## 3. 两层接入

| | 基础接入 | 完整接入 |
| --- | --- | --- |
| 要做什么 | 选平台、填平台上的 Agent id 和自己的 API Key | 再签一把 Matrix 密钥，填进平台上这个 Agent 的 MCP 设置 |
| 能做什么 | 被 @、接委托、交付（只用文本） | 加上 Matrix 的网络工具：查成员、委托、档案工具…… |
| 能力声明 | 全部权限 `advisory`（Matrix 管不到它在平台上做什么） | `delegate` 由网络强制，其余照旧 `advisory` |

有效权限照不变量 1 只收窄：自身权限 ∩ 委托授予 ∩ 运行时能强制的。`advisory` 的权限记进委托的收窄原因。

## 4. 一轮怎么跑

```
 Matrix                                                     公司 AI 平台
 _invoke → RemoteCall（平台、Agent id、请求头）
 RemoteRuntime.run ── POST /agents/{a}/chat {message, session_id} ─────▶ 建会话或续接
                    ◀── SSE: session{id} → delta → tool → done ───────── 
   session 一到：Bindings.bind(Matrix Agent, 平台会话 id → 这一轮的网络工具)
 /mcp/remote ◀── Authorization: Bearer <Matrix 密钥>，X-Session-Id: <平台会话 id> ── 平台 Agent 调 Matrix 的工具
   凭密钥认出是哪个 Matrix Agent，凭会话 id 落到这一轮（tools/call 还没绑定就等 bind_wait_s）
 一轮结束 / 超时 / 人终止：解除绑定；超时与终止时调 POST /sessions/{id}/interrupt
```

- **会话**：每个席位、每个委托各对应一个平台会话（存在 `dsh_session_id` 里），后续轮次续接。
- **先等再绑**：带着陌生会话 id 的回调挂起，直到某一轮的流报出这个 id；等到 `bind_wait_s` 还没有就拒绝。
  不会认错：会话 id 只由发起那一轮的流报出来。在平台上直接聊天产生的会话 Matrix 不认识，永远等不到。
- **试连不要会话 id**：平台保存 MCP 配置时会先调 `initialize` / `tools/list` 试连，那时不在任何会话里。这两个和 `ping` 不执行任何东西，只要密钥对就回答、不等待；`tools/list` 没落到某一轮时给全部网络工具的说明，落到某一轮时给这一轮能用的。**只有 `tools/call`** 必须带 Matrix 发起、还没结束的会话 id。试连不算“连上过”，界面上的“完整接入”要等第一次落到某一轮的调用。
- **结束即失效**：一轮结束就解除绑定，晚到的回调（包括停了以后还在跑的）一律拒绝。
- **并发**：同一个平台 Agent 可以同时跑多个席位，按会话 id 区分，不用串行。
- **两种传输，同一个地址**：`POST /mcp/remote` 是 Streamable HTTP（一次一条 JSON-RPC，响应就在这次 HTTP 响应里）；
  只支持旧版传输（MCP 2024-11-05 的 HTTP+SSE）的平台 `GET /mcp/remote` 开一条 SSE 流，第一条 `endpoint` 事件给出这条流专用的
  消息地址 `/mcp/remote/messages?session_id=…`，JSON-RPC 发到那里回 202、响应作为 `message` 事件从流里回来（`matrix/mcp_sse.py`）。
  两种走同一个 `remote_mcp`，密钥与会话规则一样：开流就要密钥对（不对 401），每个平台 Agent 同时最多 20 条流；
  消息地址只对开着的流有效，流断了 404；POST 没带密钥就沿用开流时的，带了就必须是同一个 Agent 的；
  平台会话 id 先看这次 POST 的请求头，没有再用开流时的——所以平台要么每次 POST 都带会话 id，要么每个会话各开一条流。
  空闲时每 15 秒发一行注释保活。
- **传文件（暂存 + `file_id`；取文件按 sha）**：平台 Agent 的工作目录在平台上，Matrix 读不到也挂不进去，文件一律经这两个接口进出，
  请求头和调 MCP 一样（同一把密钥 + 同一个会话 id），**只在 Matrix 发起、还没结束的那一轮里有效**：
  - **发**：平台 `POST /mcp/remote/files?name=<文件名>`（请求体就是文件内容，类型取 `Content-Type`，不限类型）拿回
    `{file_id, name, size, sha256}`，同一轮里：
    - `send_file(file_id=…)`：席位里附在这一轮的回复上发给人（同 [conversations.md](reference/conversations.md)“Agent 发文件给人”）；
      **委托任务里**（外部平台执行者的任务也有 `send_file`）随交付交给委托方：存进对话的附件区，记在 `Task.pending_files`，
      交付时（交付要所有者确认的，确认之后，不变量 22）并进 `Task.sent_files`，交付通知里列出文件名；不进群聊。
    - `delegate` 的 `attachments` 写 file_id（每行一个，可以和 `attachments/…` 路径混写）：存进对话的附件区，转交给受托方。
  - **取**：`GET /mcp/remote/files/<sha256>`，只给这一轮本来拿得到的，口径同云侧挂进工作目录 `attachments/` 的（`_attachments_for`）：
    席位是对话里它看得到的消息上的附件；委托任务是委托方转交的；再加它发起、**已经交付**的委托里执行者发来的（只给直接委托方）。
    别的一律 404。下载一律以附件返回（`Content-Disposition: attachment`、`nosniff`），响应头 `X-File-Sha256`。
    Matrix 在外部 Agent 的提示词末尾加一节“文件（你在外部平台上）”，列出这一轮拿得到的每个文件的名称、类型、大小和取文件地址，
    并说明怎么发（`protocol.format_remote_files`）。
  - 所以两个外部平台 Agent 之间：群聊里 A `send_file` 的，B 下次被唤醒时取得到；A 委托 B 时把 file_id 写进 `attachments`，
    B 在任务里取得到；B 在任务里 `send_file` 的，A 收到交付的那一轮取得到。
  - 规则同 `tools/call`：密钥不对 401、没有会话 id 400、不是 Matrix 发起或那一轮已经结束 403（陌生会话照样先等 `bind_wait_s`）；
    暂存时这一轮既没有 `send_file` 也没有 `delegate` 也 403。**先认密钥与这一轮，再收请求体**。
  - 单个 ≤20 MB（同附件，超了 413），一轮最多暂存 20 个（429），一次回复最多附 10 个；`send_file` 的信息流检查照旧（不变量 22）。
  - 暂存只属于那一轮：存在 `<data>/staging/<file_id>`，`file_id` 只在那一轮里认，一轮结束（或服务重启）就删；
    发出去、转交出去的已经拷进对话的附件区。签密钥时返回的 `files_path` 就是这个地址。
  - 传和取都要由**平台**做（它的文件处理 / 代码沙箱），不是模型：密钥配在平台的 MCP 设置里，模型看不到，也不该看到。

## 5. 凭证与调用方授权

- **平台 API Key**：添加它的人填的，只进保管库（`vault/remote-<Agent id>`），每轮发起会话时放进部署方配置的请求头。
  不进 `state.json`、事件、日志、API 返回。
- **Matrix 密钥**（`mrk_…`）：只在签发时返回一次，`state.json` 里只有 sha256；重签旧的立刻作废。它只证明“是这个 Agent 在调”，
  单有它不够：还要带一个 Matrix 发起、还没结束的会话 id。
- **调用方授权透传**：平台目录里写了 `caller` 的，网络取**这一轮调用方**（不变量 27 的 `principal`；个人 Agent 就是所有者）
  自己授权的那个目录连接器的字段，放进 `caller.header`。他没授权就**不发起会话**，在对话里说明要先连接；
  从不退回到添加它的人或维护人的授权。一轮里有几个人同时 @ 它时说不清用谁的，这一轮不发起。

## 6. 和别的规则的关系

- **不变量 10（身份由网络认定）**：平台回调的身份 = 密钥认出的 Agent + 会话 id 绑定的那一轮的执行上下文令牌；不信模型自报，
  会话 id 由平台填、不由模型填（接入验收要确认，见接入指南）。
- **不变量 27（公共 Agent）**：外部平台的 Agent 也能设成公共；服务面的调用方授权经 `caller` 透传。平台那边会不会跨用户保留记忆
  Matrix 管不到：管理员批准时要确认（[public-agents.md](public-agents.md) 第 7 节）。
- **不变量 22（信息流）**：群聊内容发到平台就是向平台披露；私有目录不挂（它的记忆在平台上）。
- **读隔离**（[security.md](reference/security.md)）：远程 Agent 不在服务器上执行，读不到 `--data`，不加重云侧的读隔离欠账。
- **A2A**：平台不说 A2A；就算以后说，也只在网络边界用，进来转成普通的席位与委托。

## 7. 第一版的实现

| 部分 | 在哪里 |
| --- | --- |
| 平台目录 | `matrix/remote_agents.py` 的 `PlatformCatalog`（`<data>/agent-platforms.json`，`--platforms` / `MATRIX_PLATFORMS`，热加载；写坏了沿用上一份） |
| 驱动 | `Driver` 接口（`describe` / `chat` / `interrupt`）；内置 `StandardDriver`（标准 HTTP 形状，见 `remote_agents.py` 开头）；别的经 entry point `matrix.agent_platforms` |
| 运行时 | `RemoteRuntime`：读流、转事件、超时与终止时中断、结束时解除绑定；`remote_agents.capabilities` |
| 绑定 | `Bindings`（按（Matrix Agent, 平台会话 id）绑定这一轮的 `ToolBridge`，带等待） |
| 引擎 | `AgentNode.remote`；`add_remote_agent`、`set_remote_api_key`、`issue_remote_key`、`_remote_call`（凭证与调用方请求头）、`remote_mcp`（JSON-RPC：initialize / ping / tools/list / tools/call）；`runtime_for`、`capabilities`、`home_mounted` |
| 接口 | `GET /api/agent-platforms`；`POST /api/remote-agents`；`PUT /api/agents/{id}/remote/api-key`；`POST /api/agents/{id}/remote/key`；`POST /mcp/remote`（Streamable HTTP）；`POST /mcp/remote/files`（暂存要发的文件，`stage_remote_file`）；`GET /mcp/remote/files/{sha}`（取这一轮拿得到的文件，`remote_file`）；`GET /mcp/remote` + `POST /mcp/remote/messages`（旧版 HTTP+SSE，`matrix/mcp_sse.py`）；都不走登录，反向代理上也不加额外的认证层（[reverse-proxy.md](reverse-proxy.md)） |
| 界面 | Agent 页“接入外部平台的 Agent”；外部 Agent 的设置里“外部平台”一节（接入状态、签 / 重签密钥、换 API Key） |
| 样板与测试 | `examples/remote-agent/demo_platform.py`（演示平台）；`tests/test_remote_agents.py` |

**第一版的限制**：

- 基础接入只送文本：附件、项目文件、技能正文平台拿不到。完整接入时附件与平台产出的文件经第 4 节“传文件”进出，项目文件用档案工具读；
  项目档案文件、技能正文还不能按文件取。
- 运行中提问（`ask_user_question`）、工作区外溢审批都没有：平台自己的权限弹窗转不回来。
- Matrix 的连接器不经 `/mcp/remote` 提供；要用户鉴权的系统走调用方授权透传。
- 平台上 Agent 的工具名前缀可能和提示词里写的 `mcp__matrix_net__*` 不一样：模型一般能对上，对不上时要在平台的 Agent 说明里写一句。
- `/mcp/remote` 没有单独限频；密钥不对的请求不等待、直接 401。
