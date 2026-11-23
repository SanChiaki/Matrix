# 外部平台 Agent 样板

`demo_platform.py` 是一个虚构的“公司 AI 平台”，照 Matrix 的标准 HTTP 形状提供云端 Agent，用来本地验证接入，
也是接真实平台时对照的样板。设计见 [docs/remote-agents.md](../../docs/remote-agents.md)，接入步骤见
[docs/remote-agent-authoring.md](../../docs/remote-agent-authoring.md)。

## 跑起来

```bash
uv run python examples/remote-agent/demo_platform.py --port 8801
```

```bash
uv run matrix --runtime scripted --platforms examples/remote-agent/agent-platforms.json
```

1. 在 Matrix 的 Agent 页点“接入外部平台的 Agent”：平台“演示平台”，Agent id `expense`，API Key `demo-platform-key`。
2. 在它的设置里点“签 MCP 密钥”，复制请求头那一行（`Authorization: Bearer mrk_…`）。
3. 扮演平台网页上的 MCP 配置（真实平台是在网页上填）：

```bash
curl -X PUT http://127.0.0.1:8801/agents/expense/mcp -H "Authorization: Bearer demo-platform-key" -H "Content-Type: application/json" -d '{"url": "http://127.0.0.1:8765/mcp/remote", "headers": {"Authorization": "Bearer mrk_粘贴你的密钥"}}'
```

4. 和它私聊或在项目里 @ 它：说“成员”（经 MCP 查成员）、“抢跑”（先调工具后报会话 id）、“慢慢来”（然后点停止，看中断）、“发文件”（平台把文件暂存到 `/mcp/remote/files` 拿 `file_id`，再 `send_file`，回复上带附件）。

演示 Agent 最后一句会说收没收到调用方的授权（`X-User-Token`）：平台目录里加 `caller` 配置后可以看到透传。

测试见 `tests/test_remote_agents.py`（直接用这个演示平台）。
