# 跨平台的文件（设计与第一版实现）

> 状态：**第一版已实现**（2026-11-27）。外部平台 Agent 的整体设计见 [remote-agents.md](remote-agents.md)，
> 接平台的步骤见 [remote-agent-authoring.md](remote-agent-authoring.md)。实现：`matrix/file_sources.py`、
> `matrix/remote_agents.py`（平台目录的 `files`、驱动的 `files_in` / `upload`、`file` 事件）、`matrix/engine.py`
> （“跨平台的文件”一节）；测试 `tests/test_cross_platform_files.py`；样板 `examples/remote-agent/demo_platform.py`。

## 1. 要解决什么

不同平台的 Agent 传递和接收文件的方式不同：

- 对话接口的文件入参不同：有的只认链接，有的要先传到平台自己的文件接口、对话里写平台的 file_id；
- 有的不能直接把文件交回来，只能放进第三方暂存，回答里给一个文件 id 或一条临时链接。

原来 Matrix 只有一种做法：在提示词里列出文件，平台带 MCP 密钥 `GET /mcp/remote/files/<sha>` 来取；
平台产出的文件由平台 `POST /mcp/remote/files` 暂存、模型 `send_file(file_id=…)` 发出。它假设平台能带自定义请求头主动拉、能主动推，
上面两种情况都接不住。

## 2. 原则

1. **Matrix 里的文件只有一种**：对话附件区里按 sha256 存、带可见性（不变量 13）与信息流检查（不变量 22）的附件。
   差异只在边上消化：部署方的目录（来源目录、平台目录的 `files`）和平台驱动。引擎、附件区、信息流检查不按平台分支。
2. **地址和凭证都不交给模型**：模型只说“哪个来源（部署方登记的 id）、哪个文件（ref）”，
   取文件的地址由来源模板算出来，凭证由 Matrix 带上。搬运字节的是 Matrix 或平台，不是模型（同 remote-agents.md 第 4 节）。
3. **收进来、不转发链接**：第三方的链接会过期，而且谁拿到谁能打开，会绕过对话的可见性。拉进 Matrix 后由 Matrix 管谁能看、留多久。
4. **只向已经接受的地方披露**：Matrix 不把附件传到第三方存储。交给平台的只有这一轮本来就拿得到的文件（`_attachments_for`，
   同 `GET /mcp/remote/files/<sha>`），去处只有平台自己的文件接口或 Matrix 自己签的链接——和“把群聊内容发给平台”是同一次披露。
5. **进来的文件照 send_file 的规矩出去**：单个 ≤20 MB、一轮暂存 ≤20 个、一次回复 ≤10 个、信息流检查照旧。

## 3. 四条路径

| 情况 | 怎么处理 | 谁来做 |
| --- | --- | --- |
| 平台回答里给自己的文件 id / 第三方暂存的临时链接 | `fetch_file(source, ref)` 拉进这一轮的暂存区，再 `send_file(file_id)` 或写进 `delegate` 的 `attachments` | 模型调工具，Matrix 取 |
| 平台在流里报出产出的文件 | 标准形状的 `file {ref, name}` 事件：按平台目录的 `files.source` 取回，照 `send_file` 附在这一轮的回复上 | 驱动 + Matrix（**基础接入也行**） |
| 平台的对话接口按入参收文件 | 平台目录的 `files.in`：`url` 附 Matrix 签的短期链接，`upload` 先传到平台的文件接口、附平台的 file_id | 驱动（发起这一轮时） |
| 平台上的工具按 file_id / 链接读文件 | `export_file(file)` 按 `files.export` 返回短期链接或平台的 file_id | 模型调工具，Matrix 交 |

原来的两条照旧：平台带请求头来取（`files.in = pull`，默认）、平台主动推（`POST /mcp/remote/files`）。

为什么“对话入参”不用工具：模型在这一轮中途调工具时，对话请求已经发出去了，来不及附上；文件要进模型的上下文（文档、图片输入）
只能在发起时附。为什么“回答里的文件 id”不用驱动从正文里提取：模型知道自己产出了哪个文件，直接说比部署方写正则可靠，
平台一改输出格式正则就失效。

## 4. 外部文件来源（`file-sources.json`）

部署方维护，默认 `<data>/file-sources.json`（`--file-sources` / `MATRIX_FILE_SOURCES`），改了就生效，写坏了沿用上一份：

```json
{"sources": [
  {"id": "corp-ai", "name": "公司 AI 平台的文件", "url": "https://ai.corp.example/api/files/{ref}/content",
   "auth": "platform", "platform": "corp-ai", "hint": "平台回答里给的文件 id（file-…）"},
  {"id": "oss", "name": "对象存储的临时链接", "url": "{ref}", "hosts": ["oss.corp.example"],
   "hint": "回答里 https://oss.corp.example/… 开头的下载链接"}
]}
```

| 字段 | 说明 |
| --- | --- |
| `url` | 取文件的地址模板，`{ref}` 换成 URL 编码过的 ref；整个就是 `{ref}` 时 ref 是一条完整的链接 |
| `hosts` | 每一跳（含重定向）都必须落在这里：域名含子域名，也可以写 IP / 网段，可以是内网。不在里面的域名**连解析都不做**。不写时取 `url` 模板的主机；`url` 是 `{ref}` 时必须写 |
| `ref` | ref 要整个匹配的正则。默认：模板来源只收一个 id（`[A-Za-z0-9._~-]{1,200}`，路径穿不出去），链接来源只收 http(s) 链接 |
| `auth` | `none`（默认，不带任何凭证）或 `platform`：带外部平台 Agent 发起会话用的平台凭证，只给那个平台的 Agent 用（`platform` 写平台 id）。**凭证只发给第一跳的主机**，重定向到别的主机（预签名链接常见）时不带 |
| `max_mb` | 单个文件上限，不超过附件上限 20 MB |
| `hint` | 写进提示词，告诉模型这个来源的 ref 长什么样 |

- 连接固定在校验过的地址上、每一跳重新校验（复用 `web_fetch.py` 的做法，`web_fetch.fetch_file`），最多 5 跳；不跟 URL 里的账号密码。
- 文件名取 `Content-Disposition`（`filename*` 优先），没有就取地址的最后一段；模型可以用 `name` 改。类型取 `Content-Type`。
- 和 `web_fetch` 的区别：`web_fetch` 是给模型读网页的（只读文本、能抓公网、`web.research` 权限），`fetch_file` 只从登记的来源取文件，
  可以带部署方指定的凭证，所以不能写任意地址。

## 5. `fetch_file(source, ref, name?)`

- **什么时候有**：这个 Agent 有能用的来源（不带凭证的谁都能用；带平台凭证的只给那个平台的 Agent），**且**这一轮有 `send_file`
  或 `delegate`（拉来了要发得出去）。云侧、端侧、外部平台都一样。由网络按这一轮加（`ExecContext.file_tools`），不按执行上下文的种类给。
- **做什么**：取回来放进这一轮的暂存区（和 `/mcp/remote/files` 同一个，`stage_remote_file`），返回 file_id；之后的用法、上限、
  一轮结束就删，都和平台主动推的一样。`send_file` 与 `delegate` 的 `attachments` 本来就认 `stg_…`，不分是谁暂存的。
- 提示词里多一节“外部文件来源”，列出这一轮能用的来源 id、名字和 `hint`。
- 不算“会让别人看到内容”的工具（`OUTBOUND_TOOLS`）：它只往 Matrix 里拉，进暂存区；往外发要再过 `send_file` 的检查。
  所以就算模型被提示词注入诱导去拉某个文件，最坏也只是暂存区里多一个会被删掉的文件。
- 记一条 `file.fetched` 事件（来源、文件名、大小）。

## 6. 平台目录的 `files`

`agent-platforms.json` 里每个平台可以加：

```json
"files": {"in": "url", "matrix_url": "https://matrix.corp.example", "source": "corp-ai", "export": "upload"}
```

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `in` | `pull`（默认）/ `url` / `upload` | 这一轮拿得到的文件怎么交给平台。`pull`：提示词里列出，平台带 MCP 请求头来取（原来的做法）。`url`：对话请求的 `files` 里附短期链接。`upload`：先 `POST {url}/files?name=` 传到平台，`files` 里附平台的 file_id；同一个 Agent 传过的同一份内容不再传（只记在内存里，重启后重传） |
| `matrix_url` | http(s) 地址 | 平台访问 Matrix 用的地址，签短期链接用。`in` 或 `export` 是 `url` 时必须写 |
| `source` | 来源 id | 平台在流里报 `file` 事件时，ref 按哪个来源取。没写就不取 |
| `export` | `url` / `upload` / `none` | `export_file` 的方式。不写时有 `matrix_url` 就是 `url`，否则没有这个工具 |

- 交过去的文件在提示词里写“已经随这条消息交给你的平台”，没交成的（驱动不支持、上传失败）照旧列出 `GET /mcp/remote/files/<sha>`。
- 驱动没声明支持这种方式（`files_in`）时退回 `pull`，记一条 `remote.files_fallback` 事件；上传失败记 `remote.upload_failed`。
- 不写 `files` 的平台和原来完全一样。

### 短期链接

`GET /mcp/remote/links/<令牌>`：令牌就是凭证（`lnk_` + 24 字节随机），只能取签它时指定的那一个文件，**只在签它的那一轮里有效**，
一轮结束（或服务重启）就作废。给平台带不了请求头的场合用：对话请求里的 `files[].url`、`export_file` 的返回。
响应同 `/mcp/remote/files/<sha>`（以附件返回、`nosniff`、`X-File-Sha256`），另加 `Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
反向代理照 `/mcp/remote*` 一样放行，不加额外的认证层。

### `export_file(file)`

只给配置了 `files.export`、驱动也支持的外部平台 Agent。`file` 写提示词“文件”一节里列出的 sha256，或这一轮暂存的 file_id；
返回一条短期链接（`url`）或平台的 file_id（`upload`）。用于平台默认来取（`pull`），但平台上的某个工具只认链接或平台 file_id 的情况。

## 7. 驱动与标准 HTTP 形状

驱动接口（`remote_agents.Driver`）加了三样，都是可选的，旧驱动不用改：

- `files_in: tuple[str, ...]`：支持哪些交文件的方式（`"url"`、`"upload"`）。声明了才会收到 `chat(..., files=[...])`；
- `upload(spec, name, data, mime, headers) -> str`：传到平台的文件接口，返回平台的 file_id（`"upload"` 要有它）；
- 事件 `{"type": "file", "ref": ..., "name": ...}`：平台产出的文件。

内置的 `StandardDriver` 都支持。标准形状多了：

```
POST {url}/agents/{agent}/chat   {"message", "session_id", "files": [{name, mime, size, sha256, url | file_id}]}
     → SSE 里可选 event: file {"ref", "name"?}
POST {url}/files?name=<文件名>     请求体就是文件内容 → {"file_id"}
```

## 8. 安全与规则

- **不变量 13（可见性）**：交给平台、签成链接、`export_file` 的都只是这一轮本来拿得到的文件；取回的文件存进这个对话的附件区。
- **不变量 22（信息流）**：`fetch_file` 只拉不发；`file` 事件取回后经 `handle_tool("send_file")`，信息流检查与每轮上限一个不少。
- **不变量 28（外部平台）**：平台凭证只进保管库，带凭证的来源只给那个平台的 Agent、只发给第一跳的主机。
- **SSRF**：来源的每一跳都在部署方的 `hosts` 里，连接固定在校验过的地址上；模型只能给 ref，模板来源的 ref 默认只是一个 id。
- 短期链接是持有即可取的凭证，但只对一个文件、一轮有效；平台的日志里可能留下它，所以不做长期链接。

## 9. 没做的

- 来源的凭证只有“不带”和“平台凭证”两种；按调用方授权取（同平台目录的 `caller`）、部署方的固定密钥（要进保管库）还没做。
- `upload` 的去重只在内存里，平台那边的 file_id 过期了 Matrix 不知道（重启或换了内容才重传）。
- 平台在流里报的 `file` 事件只支持“来源 + ref”，不支持把内容内联在事件里。
- 端侧的 harness 产出的文件仍按工作目录走 `send_file(path)`；`fetch_file` 对它们只是多一个拉文件的办法。
