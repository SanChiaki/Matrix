# 编写项目插件

> 给插件作者看：从零写一个插件，到装进项目里用起来。实现细节与接口见 [reference/plugins.md](reference/plugins.md)，
> 设计取舍见 [project-plugins.md](project-plugins.md)。完整的例子是 [examples/plugins/sales-pipeline](../examples/plugins/sales-pipeline)（销售漏斗）。

插件给一个项目接上某种业务。它可以带四样东西，都是可选的，至少要有一样：

| | 是什么 | 例子 |
| --- | --- | --- |
| **表** | 插件自己的数据，按你声明的字段校验，是项目档案的一部分 | 线索、候选人、选题 |
| **页面** | 项目页上的标签页，你自己写 HTML / JS | 看板、表单、日历 |
| **技能** | 给 Agent 的做法说明（技能），告诉它这类事怎么推进 | “新线索怎么判断质量” |
| **触发** | 某件事发生时，在项目群聊里 @ 一个成员 | 外部表单进来一条、每周一 9 点、阶段变成“报价” |

插件**不编排流程**：触发只是把事情交给某个成员，之后由 Agent 按技能自己判断、自己做或委托别人。插件代码也**不在服务器上跑**：
页面只在每个看项目的人的浏览器沙箱里运行，读写都经 Matrix，按看页面的人自己的权限执行。

## 1. 五分钟上手

建一个文件夹，放两个文件：

```
todo-board/
  plugin.json
  page/index.html
```

`plugin.json`：

```json
{
  "name": "todo-board",
  "version": "0.1.0",
  "title": "待办",
  "description": "项目里的待办清单",
  "tabs": [{"key": "board", "label": "待办", "entry": "page/index.html"}],
  "tables": {
    "todos": {"title": "待办", "fields": [
      {"key": "title", "label": "事项", "type": "text", "required": true},
      {"key": "done", "label": "完成", "type": "bool"}
    ]}
  },
  "page_permissions": ["records.read", "records.write"]
}
```

`page/index.html`：

```html
<!doctype html>
<meta charset="utf-8">
<script src="../_matrix/sdk.js"></script>
<form id="add"><input name="t" placeholder="新事项"> <button>添加</button></form>
<ul id="list"></ul>
<script>
  async function load() {
    const rows = await matrix.records.list("todos");
    list.innerHTML = rows.map((r) =>
      `<li><label><input type="checkbox" data-id="${r.id}" ${r.fields.done ? "checked" : ""}> ${r.fields.title}</label></li>`).join("");
  }
  add.onsubmit = async (e) => {
    e.preventDefault();
    await matrix.records.create("todos", { title: add.t.value });
    add.reset();
  };
  list.onchange = (e) => matrix.records.update("todos", e.target.dataset.id, { done: e.target.checked });
  matrix.on((event) => { if (event === "records.changed") load(); });   // 别人（或 Agent）改了也会刷新
  load();
</script>
```

> 这段为了短直接把 `r.fields.title` 拼进了 HTML。真写插件时要先转义（见示例 `board.js` 里的 `esc`），
> 因为记录里的内容可能来自外部回调或 Agent。

校验，然后装进项目：

```bash
uv run matrix-plugin check todo-board
```

```bash
uv run matrix --runtime scripted --data /tmp/plugin-dev
```

打开 http://127.0.0.1:8765 ，注册（网络会给你一个助理），再建一个项目。
进项目页 →「插件」→「上传文件夹」选 `todo-board`，然后点新出现的「待办」标签页。

## 2. 包的结构

```
<插件名>/
  plugin.json            清单（必须在根目录，或唯一的一层顶层文件夹里）
  page/                  页面用到的全部文件：HTML、JS、CSS、图片、字体
  skills/<名字>/SKILL.md  技能，可带附带文件
  service/…              可选：插件服务的程序（只有部署方目录里的插件能带，见第 7 节“插件服务与挂点”）
```

- 浏览器**只能取到 `page/` 下的文件**。放在别处的 JS、CSS、图片，页面拿不到。
- 点开头的文件和目录不能放进包（上传 zip 时会被拒绝，目录会跳过）；`.DS_Store`、`__MACOSX` 会被忽略。
- 上限：整个包 ≤2 MB、≤300 个文件；每个插件 ≤5 个标签页、≤10 张表、≤20 个触发；每张表 ≤40 个字段、≤5000 条记录，单条记录 ≤32 KB。
  一个项目最多装 10 个插件。

## 3. 清单 `plugin.json`

### 顶层

| 键 | 必填 | 规则 |
| --- | --- | --- |
| `name` | 是 | 小写字母、数字、`-`，不以 `-` 开头结尾，≤64。项目里唯一，也是数据目录名，**发布后不要改** |
| `version` | 是 | `1`、`1.0`、`1.0.0`，可带后缀如 `1.0.0-beta.1`。升级靠它区分 |
| `title` | 否 | 给人看的名字，≤40，默认等于 `name` |
| `description` | 是 | 一句话说明，≤500 |
| `tabs` | 否 | 标签页，见下 |
| `tables` | 否 | 表，见下 |
| `page_permissions` | 否 | 页面要用的能力，见下 |
| `agent_tools` | 否 | 开给 Agent 的记录工具：`records.read`、`records.write` |
| `triggers` | 否 | 触发，见第 6 节 |
| `release_views` | 否 | 能在**发布物**里打开的标签 key（必须在 `tabs` 里），见第 4 节“在发布物里” |
| `service` | 否 | 插件服务：网络托管的 MCP 进程，见“插件服务与挂点”一节。**只有部署方目录里的插件能带**，上传的包带了会被拒绝 |
| `hooks` | 否 | 挂点：`mount` / `context` / `after`，见“插件服务与挂点”一节 |
| `connectors` | 否 | 插件用到的外部服务：连接器目录里的 id（如 `feishu`）或电脑上配好的服务名，最多 10 个，每个 ≤80。只是声明：装上并启用后出现在项目的连接器面板里，让成员看到要连什么；不授予任何访问权限 |

表名、字段名、标签 key、触发 key 都是同一种写法：小写字母开头，小写字母、数字、下划线，≤40（注意和插件名不同，**用 `_` 不用 `-`**）。

### `tabs`

```json
"tabs": [{"key": "board", "label": "看板", "entry": "page/index.html"}]
```

`label` ≤20 字，显示在项目页的标签上；`entry` 必须是 `page/` 下的 `.html` 文件，并且在包里。

### `tables`

```json
"tables": {
  "leads": {"title": "线索", "fields": [
    {"key": "company", "label": "公司", "type": "text", "required": true},
    {"key": "stage", "label": "阶段", "type": "select", "options": ["线索", "沟通", "报价"]}
  ]}
}
```

字段类型，以及写入时接受什么、存成什么：

| `type` | 写入时接受 | 存成 |
| --- | --- | --- |
| `text` | 任意文本，≤500 字，连续空白合并成一个空格 | 字符串 |
| `longtext` | 任意文本，≤8000 字，保留换行 | 字符串 |
| `number` | 数字或能转成数字的字符串（`"12"`） | 整数或小数 |
| `date` | `2026-10-23`、`2026-10-23 09:30`、`2026-10-23T09:30:00` | 原样字符串 |
| `bool` | `true` / `false`，或 `"是"`、`"否"`、`"1"`、`"0"`、`"yes"`、`"no"` | 布尔 |
| `select` | `options` 里的一个值（必须写 `options`，≤50 个、不重复） | 字符串 |
| `agent` | 项目成员 Agent 的**名字或 id** | 成员 id（用 `matrix.members()` 换回名字） |
| `file` | 项目档案里一个文件的名字 | 文件名 |

- `required: true` 的字段新增时必须给；修改时只校验你给出的字段。
- 写 `null` 或 `""` 表示清空（必填字段不能清空）。
- 清单里没有的字段一律拒绝。`id` 是保留字，不能当字段名。

### `page_permissions`：页面能做什么

只申请用得到的：安装时群聊里会公开列出插件申请了什么，主持人据此决定装不装。没申请的调用会被拒绝并留痕。

| 能力 | 解锁的 SDK 方法 | 安装说明里写成 |
| --- | --- | --- |
| `records.read` | `records.list` | 读插件自己的表 |
| `records.write` | `records.create` / `update` / `remove` | 写插件自己的表（按清单校验字段） |
| `archive.read` | `archive.read`、`archive.file` | 读项目档案（纲要、决定、文件列表） |
| `members.read` | `members` | 读成员 Agent 的名片 |
| `chat.read` | `chat.read` | 读项目群聊里的公开消息 |
| `chat.post` | `chat.post` | 以看页面的人的名义在群聊里发消息（每次确认） |
| `requests.read` | `requests` | 读项目里的用户任务（按看的人能看到的） |
| `service.read` | `service.call` | 调插件服务的只读工具（清单里标 `read` 的） |

`context`、`ui.badge`、`ui.openFile` 不需要申请。

### `agent_tools`：Agent 能做什么

写了 `records.read`，项目里的 Agent 就有“查记录”工具；写了 `records.write`，就能新增和修改记录（不能删除）。
Agent 每轮拿到的项目档案里会列出你的表和字段，所以**字段的 `label` 要写得让 Agent 看得懂**。
Agent 写的记录在页面上照常显示、照常发 `records.changed`，但**不会引发“记录变化”触发**（Agent 之间不能借插件互相唤醒）。

## 4. 页面与 SDK

### 沙箱里的规则

页面跑在一个隔离的 iframe 里，先知道这些就不会踩坑：

- **不能联网**：`fetch`、XHR、WebSocket 都会被拦下，包括请求 Matrix 自己的接口。读写一律用 `matrix.*`。
  外部系统的数据要进来，就配一个外部回调触发，让它写成记录。
- **不能引外部资源**：CDN 上的脚本、样式、字体、图片都加载不了。用到的库下载进 `page/`，用相对路径引用。
- **拿不到登录信息**，也碰不到外面的项目页；`localStorage`、cookie 都用不了。要保存的东西写进表里。
- 不能弹窗（`window.open`、`alert` 都没有效果），也不能让整个页面跳走；要提示就画在自己的页面里。
- 内联 `<script>` 和 `<style>` 可以用。
- 表单照常用：`onsubmit` 里 `e.preventDefault()`，再用 `matrix.*` 写。表单本身提交不到任何地方（没拦住也只是什么都不发生）。
- 页面地址里带一个 10 分钟有效的令牌。页面打开时就把要用的文件都加载好；过了 10 分钟再动态加载新文件会失败（切换标签页回来会重新拿令牌）。

### 引入桥客户端

```html
<script src="../_matrix/sdk.js"></script>
```

路径**相对页面文件**：`page/index.html` 写 `../_matrix/sdk.js`，`page/views/list.html` 写 `../../_matrix/sdk.js`。
引入后页面里有一个全局的 `matrix`。所有方法都返回 Promise；被拒绝时抛 `Error`，`message` 是中文原因
（如“插件没有申请「写插件自己的表」”“字段 stage 只能是：线索、沟通、报价”），可以直接显示给人看。

### 方法

**`matrix.context()`**：开页面时先调一次。

```js
{
  project: { id, name, kind, shared },    // shared：邀请了别人（记录对所有参与方可见）；kind 是推导出来的 "internal" | "joint"，保留兼容
  plugin: { name, version, title },
  viewer: { is_host },                    // 看页面的人是不是主持人
  tables: [{ key, title, fields: [{ key, label, type, options, required }] }],
  permissions: ["records.read", ...],     // 这个插件申请到的能力
  theme: "light" | "dark"                 // 项目页当前的深浅色
}
```

**记录**

```js
await matrix.records.list("leads", { q: "启明", where: { stage: "沟通" }, limit: 200 });
// → [记录, ...]，新的在前。q：在所有字段里找关键词；where：按字段精确匹配；limit 默认 200，最多 500
await matrix.records.create("leads", { company: "启明", stage: "线索" });   // → 新记录
await matrix.records.update("leads", id, { stage: "报价" });                // → 改后的记录（只改给出的字段）
await matrix.records.remove("leads", id);                                    // → { ok: true }
```

一条记录长这样：

```js
{
  id: "rec-1a2b3c4d",
  fields: { company: "启明", stage: "报价", owner_agent: "agent-..." },
  created_at: 1792700000.1, updated_at: 1792700100.2,       // 秒
  created_by: { user: "user-..." },                          // 或 { agent: "agent-..." }，或 { trigger: "new_lead", user: "user-..." }
  updated_by: { user: "user-...", via: "销售漏斗" }
}
```

**项目信息**

```js
await matrix.members();         // [{ id, name, description, capabilities }]：项目里的 Agent
await matrix.archive.read();    // { brief, decisions: [{ id, text, reason, at }], files: [{ name, size, updated_at }], refs: [{ name, location, note }] }
await matrix.archive.file(name);// { name, text }：只能读文本文件
await matrix.chat.read(50);     // 最近的公开消息，最多 200 条：[{ id, role, text, ts, via, who }]
await matrix.requests();        // 看的人看得到的用户任务：[{ id, title, status, agent_name, requester, ... }]
```

**以看的人的名义发群聊**

```js
const { sent } = await matrix.chat.post("@销售助理 看一下报价中的几家");
```

项目页会弹一张确认卡，写明是哪个插件、要以谁的名义发、会 @ 谁。人点“发送”才发出，`sent` 为 `true`；点“取消”为 `false`。
插件改不了确认卡的内容，也没有别的办法绕过它。发出的消息在群聊里标着“经 <插件名>”，被 @ 的 Agent 照常接任务。

**界面**

```js
matrix.ui.badge("3");          // 标签页上的小标记，≤12 字；传 "" 清掉
matrix.ui.openFile("报价单.pdf"); // 打开项目档案里这个文件的预览
```

**事件**

```js
const off = matrix.on((event, data) => {
  if (event === "records.changed") reload(data.table);   // 任何人（包括 Agent、外部回调）改了这个插件的记录
});
```

### 在发布物里

主持人可以把档案文件连同一个插件标签发布成**交付件**发布物给项目外的读者（[project-publishing.md](project-publishing.md)）。
只有清单 `release_views` 里列出的标签能这样用。读者打开时页面代码是**发布时快照的那一版**，同样跑在沙箱里，桥只有：

- `context()`：`mode` 是 `"release"`，`release` 里有标题、版本、`snapshot_at`；`tables` 为空。
- `archive.read()`：这个版本快照里的文件清单（不是项目档案）；`archive.file(name)`：读其中一个文本文件。

记录、成员、群聊、`chat.post` 都没有（调用会报错）。所以能在发布物里打开的页面，数据要从档案文件来：
例如 `examples/delivery-dashboard` 的看板页面在项目里读“看板”表找文件，在发布物里直接列快照里的 `.json`。

```js
const ctx = await matrix.context();
if (ctx.mode === "release") { /* 只读快照：archive.read / archive.file */ } else { /* 项目里：记录 + 档案 */ }
```

### 跟随深浅色

`context().theme` 是项目页当前的主题。示例的做法是 CSS 默认深色，`theme === "light"` 时给 `<html>` 加一个 `light` 类覆盖颜色变量。

## 5. 技能

`skills/<名字>/SKILL.md` 和项目技能是同一种格式：

```markdown
---
name: "sales-intake"
description: "新线索进来时怎么判断质量、下一步做什么"
---
1. 先查这条线索（plugin_records，where 用公司名）……
2. ……
```

- `name` 必须和目录名一致，并且要有 `description`。
- 插件装上后，技能对项目里的 Agent 生效，排在项目自己的技能之后（重名时项目技能优先）。
- 触发的消息里可以点名技能，例如“新线索：{company}，按 sales-intake 跟进”。
- 技能里引用工具时，Agent 看到的是 `plugin_records`（查）和 `plugin_write`（建 / 改）。参数里的 `where`、`fields` 是 JSON 字符串，
  如 `{"stage": "报价"}`。

## 6. 触发

每个触发只做一件事：在项目群聊里发 `@<notify.to> <notify.text>`，**以启用它的人的名义**。被 @ 的成员照常接任务，
它据此登记的用户任务，下发人也是启用触发的人。

```json
"triggers": [
  {"key": "new_lead", "type": "webhook", "table": "leads",
   "notify": {"to": "销售助理", "text": "新线索：{company}（{source}），按 sales-intake 跟进"}},
  {"key": "quote", "type": "record", "table": "leads", "when": {"field": "stage", "equals": "报价"},
   "notify": {"to": "销售助理", "text": "{company} 进入报价阶段，请准备方案"}},
  {"key": "first", "type": "record", "table": "leads", "when": {"event": "create"},
   "notify": {"to": "销售助理", "text": "新建了线索 {company}"}},
  {"key": "weekly", "type": "schedule", "cron": "0 9 * * 1",
   "notify": {"to": "销售助理", "text": "整理上周漏斗变化，发到群聊"}}
]
```

- **`notify.to`** 是要 @ 的成员**名字**，必须和项目里某个 Agent 的名字完全一致。装之前没法验证，所以名字最好通用
  （如“销售助理”），并在 `description` 里写明需要这样一个成员。
- **`notify.text`** 里的 `{字段}` 会换成记录的值（每个值最多 200 字）；没有的字段换成空。
- 触发装上后**默认不开**，主持人在「插件」页逐个启用；谁启用，就以谁的名义发。
- 失败（要 @ 的成员不在项目里、启用的人已离开项目）会记下原因，连续 3 次自动暂停，主持人重新启用后恢复。

### `webhook`：外部回调

主持人在「插件」页点“生成地址”，拿到一个只显示一次的地址，配到外部系统（表单、工单、CRM）里。外部系统这样调用：

```bash
curl -X POST http://127.0.0.1:8765/hooks/h_xxxxxxxx -H 'content-type: application/json' -d '{"company": "启明", "source": "官网表单"}'
```

- 写了 `table`：请求体按字段名映射成一条新记录（清单以外的键丢弃，照常校验），`{字段}` 取这条记录的值。
- 没写 `table`：不建记录，`{键}` 直接取请求体里的文本或数字。
- 返回 `{"ok": true, "record": "rec-…", "message": "msg-…"}`。请求体是 JSON，≤64 KB；每个地址每分钟最多 60 次。
- 错误码：没启用 409、地址不对或已重置 404、不是 JSON 400、太大 413、超频 429、字段校验不过 409（返回原因）。

### `record`：人改了记录

- `"when": {"field": "stage", "equals": "报价"}`：字段**变成**这个值的那一刻触发一次（本来就是这个值再改别的字段不触发）。
- `"when": {"event": "create"}`：新增记录时触发。
- 只有人写的记录才触发（页面上、外部回调）；Agent 写的不触发。

### `schedule`：定时

五段 cron，按服务器本地时间：`分 时 日 月 周`，支持 `*`、`1-5`、`1,15`、`*/15`；周日是 `0` 或 `7`。

| 写法 | 意思 |
| --- | --- |
| `0 9 * * 1` | 每周一 09:00 |
| `30 18 * * 1-5` | 工作日 18:30 |
| `0 10 1 * *` | 每月 1 号 10:00 |
| `*/30 * * * *` | 每 30 分钟 |

最小间隔 10 分钟（分钟这一段最多 6 个值）。定时触发没有记录，消息里的 `{字段}` 都会是空。

## 6.5 插件服务与挂点

页面、表、触发碰不到 Agent 每一轮看到什么。要做记忆、Wiki、知识库这类东西，插件需要一个**服务**和几个**挂点**
（设计见 [plugin-hooks.md](plugin-hooks.md)，完整示例 `examples/plugins/llm-wiki/`）。

**服务**是一个标准的 stdio MCP 程序（按行收发 JSON-RPC：`initialize`、`tools/list`、`tools/call`），不需要 Matrix 专用的 SDK：

```json
"service": {
  "command": "python",
  "args": ["{plugin}/service/wiki_engine.py", "--root", "{data}"],
  "tools": {"wiki_search": "read", "wiki_read": "read", "wiki_write": "write"}
}
```

- 网络按项目启动一个进程，工作目录是这个项目的数据目录（`{data}`），第一次用到时启动、空闲 15 分钟停掉。
- 进程环境里没有任何凭证：要连外部服务请走连接器。服务**不要调模型**：要模型做的事写进技能，交给项目里的 Agent。
- `tools` 是白名单和读写分级：只有列出来的工具给 Agent（经 `plugin_tools` / `plugin_call`）和页面（只有 `read` 类）；
  `write` 类要 Agent 有写权限，且会被信息流检查拦下（读过私下对话的会话不能写进项目数据）。每次调用都记日志，但不记参数和结果。
- 只能放进部署方的插件目录（`plugins.json`）：服务端执行的代码要部署方审核过。

**挂点**（都要有 `service`，读的是它的数据目录）：

```json
"hooks": {
  "mount":   [{"path": "wiki", "as": "wiki"}],
  "context": [{"file": "wiki/index.md", "budget": 3000, "label": "项目 Wiki 目录"}],
  "after":   [{"on": "request.accepted", "notify": {"to": "{agent}", "text": "「{title}」已验收，写回 wiki。交付说明：{summary}"}}]
}
```

| 挂点 | 效果 |
| --- | --- |
| `mount` | 数据目录里的子目录只读挂进 Agent 工作目录的 `plugins/<插件>/<as>/`，每轮刷新 |
| `context` | 每轮往提示词里加一段，标明来自你的插件：`file`（一个数据文件，没变就不重推）或 `tool`（调一个 read 类工具，参数模板可用 `{latest}`、`{mission}`），截到 `budget`（≤4000） |
| `after` | 项目群聊里的用户任务被验收之后：`notify` 以验收的人的名义 @ 成员（`{agent}` 是接任务的 Agent），或 `tool` 由网络调一个工具 |

这些挂点只对装了插件的项目里的运行生效，dsh、Claude Code、Codex 和云侧、端侧都一样。

## 7. 调试

### 本地校验

```bash
uv run matrix-plugin check path/to/my-plugin
```

目录或 `.zip` 都行。它做两件事：

- **装得上吗**：和安装时同一套校验，错误直接告诉你哪一项（如“表 leads 的字段 stage 是 select，要有 options”），退出码 1。
- **多半写错的地方**（提醒，不影响安装）：
  - 页面没引入桥客户端，或者路径不对；
  - 引用了外部资源、用了 `fetch` 等联网方式；
  - 用到了没申请的能力，或者申请了没用到的能力；
  - 触发消息里的 `{字段}` 会换成空；
  - 有文件放在 `page/` 外面，页面取不到。

  加 `--strict` 时提醒也算失败，适合放进 CI。

### 在本地跑起来

```bash
uv run matrix --runtime scripted --data /tmp/plugin-dev
```

`scripted` 运行时不需要模型，Agent 的回复是固定的演示内容，适合调页面、记录和触发。要看真实 Agent 怎么用你的技能和记录工具，
去掉 `--runtime scripted`（需要 PATH 里有 `dsh`）。

**改了之后怎么生效**：项目里装的是上传那一刻的拷贝。

- 上传的插件：改完再上传一次同一个文件夹（版本号不变也会覆盖），回到标签页就是新的。
- 放进部署方目录调：写一个 `plugins.json`（`{"plugins": ["path/to/my-plugin"]}`），用 `--plugins plugins.json` 启动。
  目录会自动发现变化，但项目里要“升级”才换成新的，所以每次改完把 `version` 往上加一点，卡片上会出现“升级到 …”。

### 看页面的报错

在浏览器里打开开发者工具。插件页面是一个 iframe：Chrome 在 Console 顶部的上下文下拉框里选中插件的 frame（名字是 `index.html` 之类），
就能看到它的报错、也能在里面直接试 `await matrix.records.list("leads")`。

| 现象 | 原因 |
| --- | --- |
| 标签页一片空白，Console 里 `matrix is not defined` | 桥客户端路径不对（相对页面文件） |
| Console 里 “violates the following Content Security Policy directive” | 用了外部资源或联网；改成包里的文件、用 `matrix.*` |
| 调用报“插件没有申请「…」” | `page_permissions` 里少了对应能力 |
| 调用报“表 x 没有字段”“字段 x 只能是…” | 写入的字段和清单对不上 |
| `matrix.chat.post` 一直不返回 | 确认卡在等人点；点了“取消”会返回 `{ sent: false }` |
| 触发不动 | 没启用；或已暂停（卡片上有“已暂停”和原因）；或是 Agent 写的记录 |
| 触发的消息里 @ 的人没反应 | `notify.to` 和项目里成员的名字不一致 |
| 过一会儿再点按钮，新加载的图片 / 脚本 404 | 页面令牌 10 分钟过期；资源在页面打开时就加载好 |

## 8. 升级与数据

- 加表、加字段、加选项可以直接升级。
- 删表、删字段、改字段类型、去掉选项，升级时会先列出影响，让主持人确认。旧记录原样保留，页面上不显示被删的字段。
- 不要改 `name`：它决定数据放在哪；改了就是一个新插件，旧数据不会跟过去。
- 主持人可以随时停用（数据和设置都在，重新启用即恢复）或卸载（记录保留，重新装回来还在）。

## 9. 交给别人用

- **给一个项目用**：把文件夹打成 zip，或者直接让主持人在「插件」页“上传文件夹”。
- **给整个部署用**：部署方把插件放到服务器上，写进 `plugins.json`，用 `--plugins` 启动。所有项目的主持人都能在「插件」页的目录里一键安装。

装插件的人要知道：插件作者的代码能读到它申请的那些数据，并可能经页面跳转带出去（见 [security.md](reference/security.md)）。
所以只申请需要的能力，在 `description` 里写清楚插件做什么、需要项目里有哪些成员。
