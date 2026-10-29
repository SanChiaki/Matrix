# 接入已有的看板系统（示例）

公司已经有一个看板系统，对外是 HTTP API（建看板、改看板、发布看板），而且是公开的、不设限制，地址原来写死在看板技能里。
这里按业界通行的方式把它接进 Matrix 项目：**把 HTTP API 包成一个远程 MCP 服务，登记成部署方目录里的连接器**。
看板仍然做在、放在原系统里；Matrix 项目是围绕它协作的地方。
接自己的系统时按 [部署方接入指南](../../docs/connector-authoring.md) 做，最后有上线前的检查清单。

```
Agent ──(Matrix 网络代调用)──> board_mcp.py（MCP 包装层）──HTTP──> board_api.py（看板系统）
```

| 文件 | 是什么 |
| --- | --- |
| `board_api.py` | 演示用的看板系统：公开的 HTTP API + 给人看的看板页面（`/d/<id>`，草稿 `?draft=1`） |
| `board_mcp.py` | MCP 包装层：五个工具，带 MCP 规范的工具标注。只读的标 `readOnlyHint`；建草稿、改草稿只动草稿；`publish_dashboard` 标 `openWorldHint`（发布后任何人可见） |
| `connectors.json` | 部署方目录：交付数据（查数，复用 `examples/delivery-dashboard`）+ 看板系统（不要授权；`domains` 让资料里的看板链接被认出；自带技能 `dashboard-howto`，里面**不再写地址**） |
| `setup_demo.py` | 搭演示数据：建草稿 → 链接进资料 → 说“发布” → 确认卡 → 发布 |

Matrix 这边用到的能力：

- **写操作逐次确认**（不变量 19）：部署方目录里的工具按标注明确是写操作、并且 `destructiveHint` 或 `openWorldHint` 为真的，
  每次调用前先给连接的授权人一张确认卡，照原样列出要发出的参数，同意才发、只发这一次。建草稿、改草稿、只读的工具不打断。
- **外部页面标签**：部署方在 `connectors.json` 里给看板系统写 `"embed": true`（只对它的 `domains` 生效），主持人就能在项目页
  “资料”里把看板链接“设为标签”，看板系统的页面直接嵌在项目页里。用的是看的人自己在看板系统的登录，Matrix 不读里面的内容、
  不给它任何东西。前提是看板系统允许被 Matrix 的地址嵌入（`X-Frame-Options` / CSP `frame-ancestors` 放行），否则标签里是空白，点“新窗口打开”。
- **`add_link`**：Agent 把看板链接加进项目资料，同一个地址再加就是更新说明（草稿 → 已发布 vN）。连接器面板按域名认出是看板系统。
- Agent 只要 `connector.use`，不要 `shell.exec`；地址只在部署方那里一处。

## 看效果

```bash
uv run python examples/dashboard-system/setup_demo.py
```

然后按它打印的命令起三个服务和 Matrix，用 `zhang@demo.local` / `demo-pass-123` 登录：

- 项目页上有一个“华东交付周报”标签：看板系统的页面嵌在里面。
- 项目「华东交付」的群聊里能看到整个过程；项目页“资料”里有看板链接（浏览器把 `board.localhost` 解析到本机，点开就是看板系统的页面）。
- 再说一遍 `@看板 Agent 更新看板系统里的看板` → 只改草稿，不问；`@看板 Agent 发布这个看板` → 群聊里出现给你的确认卡，
  看板“待我处理”里也有，点“发出这一次”或“不发”。

换成真实的看板系统：只改 `board_mcp.py` 里调 HTTP 的那几行（路径、请求体），以及 `connectors.json` 里的地址和 `domains`。
看板系统要是以后加了授权，在包装层按 MCP 的授权方式加上，`connectors.json` 里写授权字段，看板系统和技能都不用动。
