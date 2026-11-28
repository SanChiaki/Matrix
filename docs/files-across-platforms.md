# 跨平台的文件（设计与第一版实现）

> 状态：**第一版已实现**（2026-11-27；2026-11-28 补上一个来源给几个平台用、一个平台用几个来源、多轮里反复提到同一个文件）。外部平台 Agent 的整体设计见 [remote-agents.md](remote-agents.md)，
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

## 3. 五条路径

| 情况 | 怎么处理 | 谁来做 |
| --- | --- | --- |
| 文件 id / 临时链接在某处（平台的回答、别人的消息），Agent 要拿来用 | 模型主动调 `fetch_file(source, ref)`，这一轮中途拉进暂存区，再由它 `send_file(file_id)` 或写进 `delegate` 的 `attachments`；平台 Agent 自己、读到它回答的云侧 / 端侧 Agent 都能用 | 模型调工具（要能调 Matrix 的工具），Matrix 取 |
| 平台在流里报出产出的文件 | 标准形状的 `file {ref, name}` 事件：按平台目录的 `files.sources` 取回，照 `send_file` 附在这一轮的回复上 | 驱动 + Matrix（**基础接入也行**） |
| 平台 Agent 谁也没调工具，只在自己的回复正文里写了产出的文件 id | `files.scan`：Matrix 在这一轮结束后扫最终回复，认出这一轮新出现的 id，取回并自动附上（第 6.4 节，默认关） | Matrix 自己（**基础接入也行**） |
| 平台的对话接口按入参收文件 | 平台目录的 `files.in`：`url` 附 Matrix 签的短期链接，`upload` 先传到平台的文件接口、附平台的 file_id | 驱动（发起这一轮时） |
| 平台上的工具按 file_id / 链接读文件 | `export_file(file)` 按 `files.export` 返回短期链接或平台的 file_id | 模型调工具，Matrix 交 |

原来的两条照旧：平台带请求头来取（`files.in = pull`，默认）、平台主动推（`POST /mcp/remote/files`）。

为什么“对话入参”不用工具：模型在这一轮中途调工具时，对话请求已经发出去了，来不及附上；文件要进模型的上下文（文档、图片输入）
只能在发起时附。“回答里的文件 id”优先让模型用 `fetch_file` 明说、或让平台报 `file` 事件：模型知道自己产出了哪个文件，比从正文里认可靠；
平台两样都做不到时，再用 `files.scan` 从正文里认，它多了几道闸（第 6.4 节）。

## 4. 外部文件来源（`file-sources.json`）

部署方维护，默认 `<data>/file-sources.json`（`--file-sources` / `MATRIX_FILE_SOURCES`），改了就生效，写坏了沿用上一份：

```json
{"sources": [
  {"id": "corp-ai", "name": "公司 AI 平台的文件", "url": "https://ai.corp.example/api/files/{ref}/content",
   "ref": "file-[0-9a-f]{24}", "auth": "platform", "for": ["corp-ai"], "hint": "平台回答里给的文件 id（file-…）"},
  {"id": "oss", "name": "对象存储的临时链接", "url": "{ref}", "hosts": ["oss.corp.example"],
   "for": ["corp-ai", "biz-agent", "cloud"], "hint": "回答里 https://oss.corp.example/… 开头的下载链接"},
  {"id": "dms", "name": "文档中心", "url": "https://dms.corp.example/api/docs/{ref}", "ref": "DOC-\\d{6,}",
   "auth": "secret", "header": "X-Api-Key", "template": "{value}", "for": ["*"], "cache": false}
]}
```

一个来源回答两件事：**文件在哪**（`url`、`hosts`、`ref`）和**以谁的身份取**（`auth`）。两件事分开，同一个来源才能给几个平台用：

| 字段 | 说明 |
| --- | --- |
| `url` | 取文件的地址模板，`{ref}` 换成 URL 编码过的 ref；整个就是 `{ref}` 时 ref 是一条完整的链接 |
| `hosts` | 每一跳（含重定向）都必须落在这里：域名含子域名，也可以写 IP / 网段，可以是内网。不在里面的域名**连解析都不做**。不写时取 `url` 模板的主机；`url` 是 `{ref}` 时必须写 |
| `ref` | ref 要整个匹配的正则。默认：模板来源只收一个 id（`[A-Za-z0-9._~-]{1,200}`，路径穿不出去），链接来源只收 http(s) 链接 |
| `for` | 谁能用：平台 id、`remote`（所有外部平台的 Agent）、`cloud`（云侧）、`device`（端侧）、`*`（所有）。不带凭证的默认 `*`；带凭证的必须写。旧写法 `"platform": "x"` 等于 `"for": ["x"]` |
| `auth` | 以谁的身份取：`none`（默认）不带任何凭证；`platform` 带**发起这次取文件的那个 Agent 自己所在平台**的凭证，请求头按那个平台目录里的 `auth` 写法——同一个来源给几个平台用时各带各的，谁也拿不到别的平台的；只给外部平台的 Agent（`for` 里不能写 `cloud` / `device`）；`secret` 带部署方给这个来源存的密钥（保管库 `file-source-<id>`，`matrix file-source-secret <id>` 从标准输入写入，不进命令行、不进 JSON），请求头是 `header`（默认 `Authorization`）、写法是 `template`（默认 `Bearer {value}`）。**凭证只发给第一跳的主机**，重定向到别的主机（预签名链接常见）时不带 |
| `cache` | 同一个对话里取过的同一个 ref 再取时直接用已有的那份（默认 `true`）；ref 指向的内容会变（“最新报表”）时写 `false`（第 7 节） |
| `max_mb` | 单个文件上限，不超过附件上限 20 MB |
| `hint` | 写进提示词，告诉模型这个来源的 ref 长什么样 |

- 连接固定在校验过的地址上、每一跳重新校验（复用 `web_fetch.py` 的做法，`web_fetch.fetch_file`），最多 5 跳；不跟 URL 里的账号密码。
- 文件名取 `Content-Disposition`（`filename*` 优先），没有就取地址的最后一段；模型可以用 `name` 改。类型取 `Content-Type`。
- 和 `web_fetch` 的区别：`web_fetch` 是给模型读网页的（只读文本、能抓公网、`web.research` 权限），`fetch_file` 只从登记的来源取文件，
  可以带部署方指定的凭证，所以不能写任意地址。

### 4.1 以谁的身份取：部署方要知道的

带凭证的来源取文件时用的**不是这一轮调用方本人**的身份，登记前要想清楚：

| `auth` | 实际是谁的身份 | 意味着 |
| --- | --- | --- |
| `none` | 匿名 | 不带凭证能取到的，谁都能取 |
| `platform` | 添加这个外部 Agent 的人（他的平台 API Key） | 和平台对话本身用他的 Key 是同一个口径：共享项目里别人 @ 它时，取文件用的也是他的身份 |
| `secret` | 部署方 | **`for` 里的所有 Agent、以及这些 Agent 每一轮在场的人都能拿到**。来源系统里按人分权限（谁能看哪份文档）的，不要用它 |

- **公共 Agent 的服务面只用 `none` 的来源**（不变量 27：服务面只用调用方自己的授权）：`fetch_file` 不列、平台报的文件不按它们取。
- 按调用方本人取（同平台目录的 `caller`）还没做（第 10 节）。
- 取回的文件**发进对话之后**就是对话内容：在场的人和他们的端侧 Agent（作为材料下发）本来就拿得到，不管当初用的是哪种凭证。
  能不能这样披露，在发出去的那一刻由 `send_file` 的信息流检查管；取了没发的只在这一轮的暂存区里。

## 5. `fetch_file(source, ref, name?)`

- **什么时候有**：这个 Agent 这一轮有能用的来源（按来源的 `for`；公共 Agent 的服务面只有不带凭证的，第 4.1 节），**且**这一轮有 `send_file`
  或 `delegate`（拉来了要发得出去）。云侧、端侧、外部平台都一样。由网络按这一轮加（`ExecContext.file_tools`），不按执行上下文的种类给。
- **做什么**：取回来放进这一轮的暂存区（和 `/mcp/remote/files` 同一个，`stage_remote_file`），返回 file_id；之后的用法、上限、
  一轮结束就删，都和平台主动推的一样。`send_file` 与 `delegate` 的 `attachments` 本来就认 `stg_…`，不分是谁暂存的。
- 提示词里多一节“外部文件来源”，列出这一轮能用的来源 id、名字和 `hint`。
- 不算“会让别人看到内容”的工具（`OUTBOUND_TOOLS`）：它只往 Matrix 里拉，进暂存区；往外发要再过 `send_file` 的检查。
  所以就算模型被提示词注入诱导去拉某个文件，最坏也只是暂存区里多一个会被删掉的文件。
- 记一条 `file.fetched` 事件（来源、文件名、大小、是否用的是已有的那份）。
- 这个对话里取过的同一个（来源, ref）直接用已有的那份，`fresh=true` 一定重新取（第 7 节）。

## 6. 平台目录的 `files`

`agent-platforms.json` 里每个平台可以加：

```json
"files": {"in": "url", "matrix_url": "https://matrix.corp.example", "source": "corp-ai", "export": "upload"}
```

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `in` | `pull`（默认）/ `url` / `upload` | 这一轮拿得到的文件怎么交给平台。`pull`：提示词里列出，平台带 MCP 请求头来取（原来的做法）。`url`：对话请求的 `files` 里附短期链接。`upload`：先 `POST {url}/files?name=` 传到平台，`files` 里附平台的 file_id；这个对话里传过的同一份内容不再传（记在对话上，重启后也认，`upload_ttl_s` 过期重传） |
| `matrix_url` | http(s) 地址 | 平台访问 Matrix 用的地址，签短期链接用。`in` 或 `export` 是 `url` 时必须写 |
| `sources` | 来源 id 的列表 | 平台在流里报 `file` 事件时按哪些来源取（第 6.1 节）。只写一个时也可以写成字符串 `source`。没写就不取 |
| `repeat` | `skip`（默认）/ `attach` | 平台又报了这个 Agent 在这个对话里已经发过的文件：不再附 / 照附（第 7 节） |
| `upload_ttl_s` | 秒数 | `upload` 传过的平台 file_id 用多久，过了重传（默认 0 = 一直用） |
| `scan` | `false`（默认）/ `true` | 平台只在回复正文里写文件 id、不报 `file` 事件时打开：从这一轮的最终回复里认出 id 取回附上（第 6.4 节）。要写 `sources` |
| `resend` | `all`（默认）/ `new` | `all` 每轮交这一轮拿得到的全部；`new` 同一个平台会话里只交上一轮之后新出现的，平台会话 id 变了就再交全部（第 7 节） |
| `export` | `url` / `upload` / `none` | `export_file` 的方式。不写时有 `matrix_url` 就是 `url`，否则没有这个工具 |

- 交过去的文件在提示词里写“已经随这条消息交给你的平台”，没交成的（驱动不支持、上传失败）照旧列出 `GET /mcp/remote/files/<sha>`。
- 驱动没声明支持这种方式（`files_in`）时退回 `pull`，记一条 `remote.files_fallback` 事件；上传失败记 `remote.upload_failed`。
- 不写 `files` 的平台和原来完全一样。
- 启动时核对平台目录与来源目录（`Network.file_config_problems`，打印出来由部署方改）：`files.sources` 里写了来源目录里没有的、
  没给这个平台用的（来源的 `for`）、ref 格式写得一模一样的。两个目录各自热加载，所以只能在启动时和运行时核对，不能互相拦着不让加载。

### 6.1 一个平台用几个来源

平台报出的文件可能放在几个地方（自己的文件库、对象存储）。`files.sources` 写几个来源时，**按 ref 的格式选**：
ref 恰好匹配一个来源的 `ref` 正则才取；一个都不匹配、或匹配上几个都拒绝，不猜（`file_sources.route`）。
所以部署方给同一个平台的几个来源写能区分开的 `ref` 正则，比如 `file-[0-9a-f]{24}`、`https://oss\.corp\.example/\S+`。
链接来源默认只收 http(s) 链接、id 来源默认只收一个 id，这两类天然分得开；两个 id 来源都用默认格式时启动检查会报出来。
平台不用在事件里说是哪个来源。每个报出来的文件记一条 `remote.file` 事件（`attached` / `skipped` / `failed` 和说明，不带 ref：它可能是临时链接）。

### 6.2 短期链接

`GET /mcp/remote/links/<令牌>`：令牌就是凭证（`lnk_` + 24 字节随机），只能取签它时指定的那一个文件，**只在签它的那一轮里有效**，
一轮结束（或服务重启）就作废。给平台带不了请求头的场合用：对话请求里的 `files[].url`、`export_file` 的返回。
响应同 `/mcp/remote/files/<sha>`（以附件返回、`nosniff`、`X-File-Sha256`），另加 `Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
反向代理照 `/mcp/remote*` 一样放行，不加额外的认证层。

### 6.3 `export_file(file)`

只给配置了 `files.export`、驱动也支持的外部平台 Agent。`file` 写提示词“文件”一节里列出的 sha256，或这一轮暂存的 file_id；
返回一条短期链接（`url`）或平台的 file_id（`upload`）。用于平台默认来取（`pull`），但平台上的某个工具只认链接或平台 file_id 的情况。

### 6.4 平台只在回复正文里写了文件 id（`files.scan`）

有的平台不报 `file` 事件，只在回答里写“明细已生成，文件 id：file-…”。打开 `files.scan` 后，Matrix 在这一轮的**最终回复**里
按 `files.sources` 认出 id，取回附上（`Network._scan_reply`、`file_sources.find_refs`）。

它和 `file` 事件的区别在于“谁说这是个要发出来的文件”：事件是平台的程序明确声明的，正文是模型写的——可能是转述别人给的 id、
引用的旧文件、编出来的，也可能是被人写进文档诱导它复述的。所以多了几道闸：

- **只认这一轮上下文里没有的 id**：发给平台的提示词里本来就有的（人贴进来的、别人消息里的、交付通知里的）不是它这一轮产出的，一律不取，连请求都不发；
- **只用分得开的来源**：链接来源（链接的主机还得在 `hosts` 里，别的链接不碰、不解析）和**明确写了 `ref` 正则**的 id 来源；
  默认的 id 格式几乎匹配任何一个词，不参与扫描（启动检查会报出来）。认出的候选照旧要恰好匹配一个来源；
- **其余同 `file` 事件**：`repeat` 去重（同一轮里事件已经附过的、之前发过的都不再附）、`send_file` 的信息流检查、每次回复最多 10 个、
  公共 Agent 的服务面只用不带凭证的来源；每个候选记一条 `remote.file` 事件（`via: "scan"`）。
- 提示词里告诉平台 Agent：最终回复里写出的、这一轮新产出的文件 id 会被取回附上，不想发的文件不要写出 id。

**和 `fetch_file`、`file` 事件同时开**：可以，三者共用一套去重，不会重复附——
- 同一轮里 `file` 事件或 `send_file` 已经附过的，扫描时跳过；之前几轮发过的按 `repeat` 跳过；
- **模型这一轮自己 `fetch_file` 过的 id，扫描不替它附**：取来是发、是转交给委托的成员还是不发，由它决定
  （否则一句“已把 file-x 转给 B”就会把只想转交的文件附到群聊回复上）。模型在这一轮里取的记在 `ExecContext.fetched`。

平台能报 `file` 事件就用事件；`scan` 是兜底，默认关。

## 7. 多轮对话里反复提到同一个文件

每个对话记一份“文件 id → 附件”（`Conversation.file_refs`，最多 500 条，最早的先丢）：
取过的（来源, ref）、发出去或转交出去的暂存 file_id，各指向它们存进对话附件区的那一份。

| 情况 | 怎么处理 |
| --- | --- |
| `fetch_file` 又取同一个（来源, ref） | 直接从附件区放进这一轮的暂存区，不访问来源：平台的临时链接过期了、平台那边删了都不影响。`fresh=true` 或来源 `cache: false` 时重新取 |
| 下一轮写上一轮的暂存 file_id（`stg_…`） | `send_file`、`delegate` 的 `attachments`、`export_file` 都认它当时发出去的那份附件 |
| 取来只转交给委托的成员（`delegate` 的 `attachments`） | 文件只挂在那条委托上（`Task.attachments`）：执行者拿得到，看得到这条委托的人在任务详情里能下载，不进群聊、别的成员拿不到；委托方自己之后几轮也还认得（它的可见范围含它发起的委托里转交出去的附件），旧的 file_id、同一个 ref 都不用再去来源取，可以再转给别人 |
| 平台每轮都把会话里的文件报一遍 | `files.repeat = skip`（默认）：这个 Agent 在这个对话里（委托任务里是这个任务）已经发过的同一份文件不再附，也不再取；`attach` 照附，但不重新取。模型自己调 `send_file` 的照发 |
| `upload` 方式每轮都要平台的 file_id | 记在对话上（`Conversation.remote_files`），重启后也认；`upload_ttl_s` 过期重传 |
| `url` / `upload` 方式每轮都交全部文件 | `files.resend = new`：同一个平台会话里只交上一轮之后新出现的，提示词里照样列出全部（标“之前已经交给过你的平台”）；平台会话 id 一变就再交全部。请求体里本来就带 `sha256`，平台自己也能按它去重 |

### 7.1 上下文里出现的文件 id：就地标注

这一轮的上下文（对话里的消息、委托的目标与输出要求、交付通知……）里出现了 Matrix 已经拿到的文件 id 时，
就地标注它就是对话里的哪份附件（`Network._annotate_file_ids`，只改发给运行时的提示词，不改消息原文）：

- 云侧、端侧、沙箱：`pf-123（Matrix 已拿到：attachments/ab12cd34/报表.md）`——这份附件本来就随这一轮下发
  （云侧拷进工作目录，端侧作为材料、按 sha 缓存），模型直接读，**端侧不用再按 id 下载一次，连接器协议也不用改**；
- 外部平台：`pf-123（Matrix 已拿到：附件 报表.md，sha256 …，见“文件”一节）`。

标哪些 id：取回过的平台文件 id 与临时链接（`fetch_file`、`file` 事件）、**传到平台得到的 file_id**（`files.in = upload`、
`export_file` 的 `upload`，记作 `up:<平台>:<file_id>`）、发出去或转交出去的暂存 file_id。短于 6 个字符的不标（容易撞上普通文字），
只标整个 id（前后紧挨着 id 或链接里会出现的字符时不标：`pf-1` 不会标进 `pf-12`）。

**可见性不放宽**：记下的 id 只在这一轮本来就拿得到那份附件时才认（口径同 `_attachments_for`）。拿不到的，外部 ref 照常去取，
旧的暂存 file_id 照常报找不到。每个对话各记各的，别的对话里取过的不算。这两项只持久化，不进事件与接口返回（里面可能有平台的临时链接）。

## 8. 驱动与标准 HTTP 形状

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

## 9. 安全与规则

- **不变量 13（可见性）**：交给平台、签成链接、`export_file` 的都只是这一轮本来拿得到的文件；取回的文件存进这个对话的附件区。
- **不变量 22（信息流）**：`fetch_file` 只拉不发；`file` 事件取回后经 `handle_tool("send_file")`，信息流检查与每轮上限一个不少。
- **不变量 28（外部平台）**：平台凭证只进保管库；带平台凭证的来源只用取文件的那个 Agent 自己所在平台的凭证、只发给第一跳的主机，
  一个来源给几个平台用时谁也拿不到别的平台的；部署方的来源密钥只进保管库。
- **SSRF**：来源的每一跳都在部署方的 `hosts` 里，连接固定在校验过的地址上；模型只能给 ref，模板来源的 ref 默认只是一个 id。
- 短期链接是持有即可取的凭证，但只对一个文件、一轮有效；平台的日志里可能留下它，所以不做长期链接。

## 10. 没做的

- 来源的凭证还没有“按调用方授权取”（同平台目录的 `caller`）。
- `upload` 的去重按对话记：同一份文件在别的对话里还会再传一次；平台那边的 file_id 提前失效了 Matrix 不知道，只能靠 `upload_ttl_s`。
- `files.resend = new` 只看平台会话 id 变没变：平台那边会话还在、却丢了之前收的文件时 Matrix 不知道，这样的平台用 `all`。
- 平台在流里报的 `file` 事件只支持“来源 + ref”，不支持把内容内联在事件里。
- 端侧的 harness 产出的文件仍按工作目录走 `send_file(path)`；`fetch_file` 对它们只是多一个拉文件的办法。
- 云侧、端侧在**同一轮里** `fetch_file` 一个新文件后，内容还不在工作目录里（下一轮作为附件才有，或靠第 7.1 节的标注）；
  要当轮就读，得随 `tool.call` 的回执把文件推进工作目录（端侧要连接器声明新能力），没做。
- `files.scan` 只扫最终回复，不扫流里的中间文本；“这一轮上下文里没有”按字面判断，模型把 id 改写了格式（加空格、拆开）就认不出。
