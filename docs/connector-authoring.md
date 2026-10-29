# 把已有系统接进 Matrix（部署方接入指南）

> 给部署方看：公司里已有一个系统（看板系统、工单系统、数据平台……），要让项目里的 Agent 用它，或者把它的页面放进项目页。
> 当前行为见 [reference/connectors-skills.md](reference/connectors-skills.md)，设计取舍见 [connectors-and-skills.md](connectors-and-skills.md)。
> 完整的例子是 [examples/dashboard-system](../examples/dashboard-system)：一个公开、不设限制的看板系统 HTTP API，
> 包成 MCP 服务后登记进来。最后一节是上线前的检查清单。

## 1. 接入方式只有一种：远程 MCP

Matrix 的云端连接器只认 **MCP（Streamable HTTP）**，不直接接 HTTP API，也不在技能里写地址。已有系统是 HTTP API 的，
在它旁边放一层**包装层**，把要给 Agent 用的几个操作包成 MCP 工具：

```
Agent ──(Matrix 网络代调用，带上授权人的凭证)──> 包装层（MCP 服务）──HTTP──> 已有系统
```

这样做换来的东西：

| | 地址写死在技能里（旧做法） | 包成 MCP、登记进连接器目录 |
| --- | --- | --- |
| 地址 | 散在每份技能里，改一次要改所有人 | 只在 `connectors.json` 一处，技能里不写 |
| Agent 要的权限 | 要能跑命令（`shell.exec`）去调 HTTP | 只要 `connector.use` |
| 凭证 | 进 Agent 的环境或提示词 | 只进 Matrix 的加密保管，运行时拿不到（不变量 19） |
| 写操作 | Agent 想发就发 | 按工具标注，发布、删除这类每次先给授权人确认 |
| 谁用、怎么用 | 看不到 | 项目的连接器面板、每个连接的调用记录 |
| 别的 AI 工具 | 各写各的 | 同一个 MCP 服务，Claude、Cursor 等也能直接接 |

包装层是**白名单**：只包你愿意让 Agent 做的操作。已有系统有“删除”接口、但你不想让 Agent 删，就不包它。

## 2. 写包装层

[`board_mcp.py`](../examples/dashboard-system/board_mcp.py) 只用 Python 标准库，一百来行，可以照着改。也可以用官方 SDK
（Python `mcp`、TypeScript `@modelcontextprotocol/sdk`），工具标注是 SDK 里的现成参数。

Matrix 用到的只有三个方法：

| 方法 | 要做的 |
| --- | --- |
| `initialize` | 回 `protocolVersion`、`capabilities: {tools: {}}`、`serverInfo`；可以在响应头里发 `Mcp-Session-Id`，Matrix 之后会带上，回 404 时它会重新初始化一次 |
| `tools/list` | 列出工具：`name`、`description`、`inputSchema`、**`annotations`**（见第 3 节）。支持 `nextCursor` 分页 |
| `tools/call` | 执行，回 `{content: [{type: "text", text: …}], isError}` |

几条写法上的要求：

- **工具说明写给 Agent 看**：`description` 写清做什么、副作用是什么（“只改草稿，已发布的不变”“发布后任何人可见”），
  参数的格式在 `inputSchema` 里写清。复杂的做法（先建草稿、等人说发布）放进连接器自带的技能，不要塞进工具说明。
- **参数先校验再拼请求**：参数会被拼进对方系统的路径或查询（`/api/dashboards/<id>`），不校验就可能被拼出别的路径
  （`../`）。按对方系统的格式校验，不对就回 `isError`。
- **返回给人用的链接**：建好、发布好的东西，返回它在原系统里给人看的地址（不是 API 地址）。Agent 会用 `add_link`
  把它加进项目资料，地址的域名写进 `domains`，Matrix 就认得出是这个系统（第 4 节）。
- **出错回 `isError: true` 和一句人话**，不要回 HTTP 错误页；Agent 会照实告诉人。
- **返回内容用文本**：Matrix 取 `content` 里的 `text`（和 `resource` 的 `text`），图片之类只记成 `[image]`；
  没有 `content` 时才用 `structuredContent`。每次调用 60 秒超时，长任务改成“提交 + 查询状态”两个工具。
- **包装层只在内网可达**，给 Matrix 的网络进程连；它带着对方系统的访问能力，不要放到公网上。

**授权怎么传**：

- 对方系统按人授权的（各人的令牌、各人看各人的数据）：在 `connectors.json` 的 `auth.fields` 里描述让人填什么、
  拼成哪个请求头；Matrix 每次调用带上**授权人自己的**请求头，包装层原样转给对方系统。数据权限跟着人走，
  包装层里**不要**放一个共享的服务账号去替所有人查。
- 对方系统是公开的、不设限制（例子里的看板系统）：`auth` 不写，用户点“授权”时不用填东西。以后对方系统加了授权，
  在包装层按 MCP 的授权方式加上、`connectors.json` 里加字段，技能和 Agent 都不用动。

## 3. 工具标注：决定哪些调用要先确认

MCP 规范给每个工具定义了标注（`annotations`）。Matrix 只看**部署方目录里的**连接器的标注，按它决定每次调用前要不要停下来，
给连接的授权人一张确认卡（卡上照原样列出要发出的参数，同意才发、只发这一次）：

| 这类工具 | 怎么标 | Matrix 怎么做 |
| --- | --- | --- |
| 只读：查询、列表、详情 | `readOnlyHint: true` | 直接调用 |
| 只改自己这边、别人看不到：建草稿、改草稿 | `readOnlyHint: false, destructiveHint: false, openWorldHint: false` | 直接调用 |
| 结果会被系统以外的人看到：发布、发消息、发邮件、对外通知 | `readOnlyHint: false, openWorldHint: true` | **每次先确认** |
| 会改掉或删掉已有内容：覆盖、删除、撤回 | `readOnlyHint: false, destructiveHint: true` | **每次先确认** |
| **没有标注** | — | **直接调用** |

要点：

- **每个工具都明确标上四项**（`readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint`），再加一个 `title`。
  规范里没标注的默认值是“会改、会影响外部”，但 Matrix 只认**明确写出来的** `destructiveHint: true` / `openWorldHint: true`，
  没标的照常调用（否则大多数现有服务每次都要确认）。所以要确认的工具，一定要标出来。
- `title` 会写在确认卡上（“要用你的「看板系统」执行「发布看板（公开）」”），写成人一眼看懂的动作，必要时把后果写进去。
- 确认卡上只有参数。只有一个 `id` 时授权人可能认不出是哪个，可以在工具说明里让 Agent 发布前先在群聊里说清发的是哪个、
  链接是什么（例子的技能就是这么写的）。
- 标注改了，Matrix 最多 5 分钟后看到（工具列表缓存）。
- 用户自己填地址接的“自定义连接器”不按标注放宽或收紧（规范要求不信来源不明的标注）；用户电脑上的连接器由那台电脑上的
  AI 工具直接调用，网络拦不到，也不在此列。

## 4. 登记进 `connectors.json`

部署方维护一份目录（默认 `<data>/connectors.json`，或 `--connectors` / `MATRIX_CONNECTORS`），改了不用重启，写坏了沿用上一份。
例子见 [examples/dashboard-system/connectors.json](../examples/dashboard-system/connectors.json)：

```json
{"connectors": [{
  "id": "boards",
  "name": "看板系统",
  "description": "公司已有的看板系统：建草稿、改草稿、发布",
  "url": "http://127.0.0.1:8792/mcp",
  "domains": ["board.localhost"],
  "embed": true,
  "skills": [{"name": "dashboard-howto", "description": "在看板系统里做看板……做看板、发布看板时先读。", "body": "# 在看板系统里做看板\n…"}]
}]}
```

| 字段 | 填什么 |
| --- | --- |
| `id` | 小写字母、数字、`-`、`_`，不超过 40 个字符。Agent 看到的工具名是 `<id>__<工具名>` |
| `name` / `description` | 用户在“我的连接器”和项目连接器面板上看到的名字和一句话 |
| `url` | 包装层的地址。由部署方负责，可以是内网地址；用户界面和 API 都不显示它 |
| `auth.fields` | 授权时让人填的字段：`key`、`label`、`secret`（只进加密保管，不回显）、`header`、`template`（`{value}` 换成填的值，如 `Bearer {value}`）。不写就是不要授权 |
| `domains` | 这个系统给人看的页面所在的域名（含子域名）。项目资料里的链接按它认出是哪个系统，出现在连接器面板里 |
| `embed` | `true`：允许把这些域名下的页面嵌进项目页（第 5 节）。必须同时有 `domains`。默认不允许 |
| `skills` | 连接器自带的技能，只在 Agent 这一轮真有这个连接器时才挂上。写**做法**：先查什么、建草稿、什么时候停下来问人；**不写地址**，按工具名指代 |

用户这边：在“我的连接器”里授权（Matrix 用填的值试连一次 `tools/list`），勾给自己的 Agent；Agent 要有 `connector.use` 权限。
共享项目里，别人 @ 我的 Agent 用到我的连接器时，第一次会先问我（和第 3 节的逐次确认是两回事，两个都会出现）。

## 5. 把系统的页面嵌进项目页（可选）

`embed: true` 之后，主持人在项目页“资料”里对这个系统的链接点“设为标签”，项目页就多一个标签，里面是那个系统的页面。
每个项目最多 5 个。Matrix 不给嵌进来的页面任何东西（没有桥、不读里面的内容）；看的人用**自己**在那个系统的登录，权限由那个系统管。

能不能嵌进来，取决于**对方系统**的响应头和登录方式：

1. **允许被 Matrix 嵌入**。默认很多系统发 `X-Frame-Options: DENY` 或 `SAMEORIGIN`，这时标签里是空白（标签栏上有“新窗口打开”）。
   对**给人看的页面**（不必是整个系统）改成：

   ```
   Content-Security-Policy: frame-ancestors 'self' https://matrix.example.com
   ```

   并去掉 `X-Frame-Options`（它没有“允许某个站点”的写法；两者都有时现代浏览器以 `frame-ancestors` 为准）。
   `https://matrix.example.com` 换成用户打开 Matrix 的地址。只放行 Matrix，不要写 `*`。
2. **协议一致**：Matrix 用 https 打开时，http 的页面会被浏览器当作混合内容挡掉，对方页面也要 https。
3. **登录态**：页面要登录的，嵌在 Matrix 里时它的 Cookie 是第三方 Cookie。要 `SameSite=None; Secure` 才会带上；
   浏览器默认拦第三方 Cookie 时（Safari、Firefox，以及用户关了第三方 Cookie 的 Chrome）仍然拿不到，只能新窗口打开。
   可选的办法：给 Cookie 加 `Partitioned`（CHIPS）让它在嵌入里单独登录一次；或者给人看的页面本来就不要登录（例子里的看板系统）。
   登录走弹窗（OAuth）是可以的，嵌入允许弹窗。
4. **不要跳出顶层**：嵌入的 iframe 不给 `allow-top-navigation`，页面里 `top.location = …` 这类“防嵌入”脚本会失败或报错，
   登录过期时跳转登录页也只能在框里跳。iframe 的其他限制：`referrerpolicy="no-referrer"`，`allow=""`（摄像头、麦克风、全屏等都没有）。
5. **`domains` 写窄**：只写给人看的页面所在的域名，不要写整个公司的根域名，否则资料里任何这个域名下的链接都能被设成标签。

被嵌入的页面和 Matrix 同在一个界面里，看的人可能把框里的内容当成 Matrix 的。所以只给可信的内部系统开 `embed`
（这个风险的取舍见 [reference/security.md](reference/security.md)）。

## 6. 在本地验证

跑一遍例子，看确认卡和嵌入是什么样：

```bash
uv run python examples/dashboard-system/setup_demo.py --fresh
```

按它打印的命令起服务和 Matrix，登录后：项目页上有一个看板标签；在群聊里让 Agent 改草稿不会问，让它发布会弹确认卡。

换成自己的系统时：

- **看工具和标注**：用 MCP Inspector（`npx @modelcontextprotocol/inspector`）连包装层，看 `tools/list` 里每个工具的
  `annotations` 是否都明确写了，要确认的那几个是不是 `openWorldHint` 或 `destructiveHint` 为 `true`。
- **看确认卡**：把包装层登记进一份测试用的 `connectors.json`，用 `--runtime scripted` 或真实运行时让 Agent 调一次写工具，
  确认卡出现、参数是你预期的、点“不发”时对方系统里确实没变。
- **看嵌入**：在浏览器里直接打开 Matrix，把链接设成标签。空白时打开开发者工具的控制台，通常是
  “Refused to display … in a frame because it set 'X-Frame-Options'” 或 “… violates the following Content Security Policy directive: frame-ancestors”。
  再换一个默认拦第三方 Cookie 的浏览器看一次登录态。

## 7. 上线前检查清单

包装层：

- [ ] 只包了愿意让 Agent 做的操作，没有顺手把删除、管理类接口也包进来
- [ ] 每个工具都明确标了 `readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint` 和 `title`
- [ ] 结果会被系统外的人看到的（发布、发送）标了 `openWorldHint: true`；会改掉、删掉已有内容的标了 `destructiveHint: true`
- [ ] 参数按对方系统的格式校验过才拼进请求
- [ ] 建好、发布好的东西返回给人看的链接，域名和 `domains` 对得上
- [ ] 出错回 `isError` 和一句人话；没有超过 60 秒的调用
- [ ] 按人授权的系统，转发的是每个人自己的凭证，包装层里没有共享的服务账号
- [ ] 包装层只在内网可达

`connectors.json`：

- [ ] `url` 指向包装层，技能里没有写任何地址
- [ ] `auth.fields` 的 `secret` 字段只用于真正的凭证
- [ ] 自带的技能写了做法与停下来问人的时机，按工具名指代
- [ ] `domains` 只写给人看的页面所在的域名

开了 `embed` 的：

- [ ] 对方页面发了 `Content-Security-Policy: frame-ancestors` 放行 Matrix 的地址，去掉了 `X-Frame-Options`
- [ ] 对方页面是 https（Matrix 是 https 时）
- [ ] 要登录的，在默认拦第三方 Cookie 的浏览器里试过；拿不到登录态时用户知道点“新窗口打开”
- [ ] 只对可信的内部系统开了 `embed`

## 8. 不支持的

- 直接登记 HTTP API、OpenAPI 描述：要先包成 MCP。
- 命令行工具、本地 stdio MCP 作为云端连接器：它们要在运行时机器上跑、凭证在本地，只能在用户自己的电脑上用。
- OAuth 授权：还没做，目前只有表单字段（个人访问令牌一类）。
- 让嵌入的页面和 Matrix 通信（拿项目信息、跳转项目页）：不给。要和项目数据交互的页面，写成项目插件（[plugin-authoring.md](plugin-authoring.md)）。
