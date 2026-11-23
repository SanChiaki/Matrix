# 接入公司内部的 Agent 平台（部署方指南）

> 设计见 [remote-agents.md](remote-agents.md)，样板见 `examples/remote-agent/`。平台的接口文档不进这个仓库：
> 照下面的清单在内网确认，照样板写驱动。改了驱动接口（`matrix/remote_agents.py` 的 `Driver`）要同步改这份指南与样板。

## 一、先和平台方确认这些

| # | 问题 | 为什么要问 | 不满足时 |
| --- | --- | --- | --- |
| 1 | 会话怎么建、怎么续接（续接时带什么） | 每个席位 / 委托对应一个平台会话 | 不能续接：每轮新会话，上下文靠 Matrix 的提示词，效果差 |
| 2 | 结果怎么回来：流式（SSE / 分块）还是轮询 | 驱动要把过程转成事件 | 轮询也能写，驱动里自己循环 |
| 3 | 会话 id 在**流的开头**出现吗（响应头或第一个事件） | MCP 回调要靠它落到这一轮 | 到结束才给：第一轮里的工具调用会超时被拒；驱动先发一条很短的开场消息建会话 |
| 4 | 有没有会话中断接口 | 人点停止、超时时要停下平台那边 | 没有：只能断开流，平台可能还会跑完 |
| 5 | Agent 的 MCP 在哪一层配：会话级 / Agent 级 / 平台级 | 决定密钥怎么配 | 平台级：分不出是哪个 Agent，不能完整接入 |
| 6 | 平台调 MCP 时请求里有没有**会话 id**，是平台填的还是模型能改的 | 不变量 28：只认 Matrix 发起的那一轮 | 没有或模型能改：不能完整接入 |
| 7 | 会话接口能不能带自定义请求头，平台 Agent 能不能把它透传给自己的工具 | 调用方授权透传 | 不能：只做不涉及用户身份的事 |
| 8 | **请求里没有调用方请求头时**，平台 Agent 的工具会不会退回到创建者的授权 | 会的话谁调用都借到创建者的权限 | 必须改成拒绝；改不了就不要配 `caller` 以外的用法，也不要设成公共 |
| 9 | 用 A 的 API Key 能不能调用 B 分享出来的 Agent | 几个人能不能各自添加同一个平台 Agent | 不能：每人在平台上复制一份 |
| 10 | 平台会不会跨会话、跨用户保留记忆 | 设成公共时要知道（不变量 27） | 会：管理员批准公共前要知道这一点 |

## 二、平台目录

部署方在 `<data>/agent-platforms.json`（或 `--platforms` / `MATRIX_PLATFORMS`）登记，改了就生效：

```json
{"platforms": [
  {"id": "corp-ai", "name": "公司 AI 平台", "url": "http://ai.corp.example/api", "driver": "corp-ai",
   "auth": {"header": "Authorization", "template": "Bearer {value}", "label": "平台 API Key"},
   "mcp_session_header": "X-Session-Id",
   "caller": {"header": "X-User-Token", "connector": "sso", "field": "token"},
   "bind_wait_s": 30, "timeout_s": 900, "proxy": "direct"}
]}
```

- `url` 只能由部署方写（可以是内网 http）。用户与 Agent 改不了它。
- `caller.connector` 必须是 `connectors.json` 里的连接器：每个人在“连接器”页授权一次，网络取他自己的那个字段透传。
- `proxy`：`direct`（默认，内网平台直连）或 `env`（照环境变量里的代理）。

## 三、照样板写驱动

平台能照**标准 HTTP 形状**（`matrix/remote_agents.py` 开头）提供接口，就用内置的 `standard`，不用写驱动。
否则写一个包，经 entry point `matrix.agent_platforms` 注册一个工厂（无参数，返回驱动对象）：

```toml
[project.entry-points."matrix.agent_platforms"]
corp-ai = "matrix_corp_ai:Driver"
```

驱动实现三个方法：

- `describe(spec, agent, headers) -> {"name", "description"}`：添加时读名片。
- `chat(spec, agent, message, session_id, headers)`：生成事件。**`{"type": "session", "id": ...}` 必须最先**，之后
  `{"type": "delta", "text"}`、`{"type": "tool", "name", "input"}`、`{"type": "done", "text"}`（最终回复）、`{"type": "error", "message"}`。
  连接失败抛 `PlatformError`。`headers` 里已经有平台凭证和调用方授权，原样带上。
- `interrupt(spec, session_id, headers)`：中断会话。

## 四、本地测

```bash
uv run python examples/remote-agent/demo_platform.py --port 8801
```

```bash
uv run matrix --runtime scripted --platforms examples/remote-agent/agent-platforms.json
```

然后在 Agent 页“接入外部平台的 Agent”：平台选“演示平台”，Agent id 填 `expense`，API Key 填 `demo-platform-key`。
签 MCP 密钥后，按 `examples/remote-agent/README.md` 把地址和密钥填进演示平台（模拟平台网页上的配置），在私聊里说“抢跑”“成员”“发文件”试。

平台 Agent 要传文件时，平台那边（不是模型）把文件 `POST` 到 `<MCP 地址>/files?name=…`，请求头和调 MCP 时一样（密钥 + 会话 id），拿回 `file_id` 后由模型调 `send_file(file_id=…)`，或写进 `delegate` 的 `attachments`；Matrix 在提示词末尾列出这一轮拿得到的文件，平台按列出的地址 `GET <MCP 地址>/files/<sha256>` 取下来放进 Agent 能读的地方（[remote-agents.md](remote-agents.md)“传文件”，演示平台的 `_stage` / `_fetch_listed` 是样板）。
你的驱动照 `tests/test_remote_agents.py` 写一份对假平台的测试。

## 五、上线

1. 平台目录写好，驱动包装进 Matrix 的运行环境（`uv pip install`，或加进部署镜像）。
2. 平台要能访问到 Matrix 的 `/mcp/remote` 与 `/mcp/remote/*`：反向代理上加了额外的认证层（如 basic auth）时要放行这两类路径，
   由 Matrix 凭密钥鉴权（[reverse-proxy.md](reverse-proxy.md)）。
3. 公司 IT 给平台开出站到 Matrix、给 Matrix 开出站到平台。

## 六、真机验收（逐条打勾）

- [ ] 添加时读到了平台上的名称与描述；API Key 不出现在 `state.json`、事件、接口返回里
- [ ] 私聊里来回两轮，第二轮续接的是同一个平台会话
- [ ] 签密钥、在平台网页上填好后，平台 Agent 能调 `list_members`；Agent 设置里显示“完整接入”
- [ ] 第一轮里平台**先**调工具、后报会话 id：调用成功（先等再绑）
- [ ] 在平台上**直接**和这个 Agent 聊天、让它调 Matrix 的工具：被拒绝
- [ ] 一轮结束后用旧会话 id 回调：被拒绝；重签密钥后旧密钥 401
- [ ] 人点“停止”：平台那边的会话被中断，之后还能续接
- [ ] 配了 `caller`：没授权的人找它，这一轮不发起、提示去连接；授权后平台收到的是**他自己的**凭证
- [ ] **不带调用方请求头直接调平台 Agent 的业务工具：业务系统拒绝**（清单第 8 条，最要紧）

## 七、安全

- 平台地址只来自目录；`/mcp/remote` 的 `tools/call` 只凭“密钥 + Matrix 发起且还没结束的会话 id”，不要放宽成只凭密钥（只有不执行东西的 `initialize` / `ping` / `tools/list` 为了平台试连不要会话 id）。
- 不要把调用方授权换成维护人 / 服务账号的授权；缺了就不发起。
- 驱动不要打印请求头（里面有凭证）；`RemoteCall.headers` 不进 repr。
