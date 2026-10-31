# 项目 Wiki（示例）

一个能装进任何项目的插件：把项目资料编译成一份由 Agent 维护的 wiki，提问先查 wiki、带出处作答，验收过的结论写回。
它是插件挂点（[设计](../../docs/plugin-hooks.md)、不变量 24）的第一个验证场景：同一个插件用上了**插件服务、插件技能、
三种挂点和插件页面**，dsh、Claude Code、Codex 上效果一样。

模式参照 Karpathy 的 LLM Wiki：原始资料不可变（`raw/`），wiki 是从原始资料编译出来、由 Agent 维护的一层（`wiki/`），
`index.md` 是目录、`log.md` 只追加；页面里的数字、日期、原话要能在引用的原始资料里原样找到。

| 部件 | 在哪 | 做什么 |
| --- | --- | --- |
| 插件服务 | `../plugins/llm-wiki/service/wiki_engine.py` | 只用标准库的标准 MCP 服务：存原始资料、读写页面（覆盖前留旧版本）、维护目录与日志、全文搜索、确定性体检（目录一致、坏链接、缺出处、数字对不上）；页面与原始资料也作为 MCP 资源（`wiki:///…`）。**不调模型**。stdio 由 Matrix 托管，或 `--http` 由部署方运行 |
| 工具 | Agent 那里 | 按原名出现：`llm-wiki__search`、`llm-wiki__write`…，带服务自己的参数说明 |
| 技能 | `../plugins/llm-wiki/skills/` | `wiki-ingest`（存原始资料 → 分诊 → 编页面 → 级联 → 收尾；验收后写回）、`wiki-query`（先查 wiki、带出处）、`wiki-lint`（能修的修、要判断的列给人） |
| `context` 挂点 | `plugin.json` | 每轮把资源 `wiki:///wiki/index.md` 推进提示词（≤3000 字，没变不重推） |
| `mount` 挂点 | `plugin.json` | `wiki:///wiki/` 下的资源只读放进工作目录 `plugins/llm-wiki/wiki/` |
| `after` 挂点 | `plugin.json` | 项目群聊里的用户任务被验收后，代表验收的人 @ 接任务的 Agent 写回 |
| 页面 | `../plugins/llm-wiki/page/` | 项目页“Wiki”标签：目录、页面、原始资料、旧版本、搜索、体检（只调只读工具） |
| 演示资料 | `materials/` | 竞品定价调研、用户访谈纪要（演示数据） |

## 看效果

```bash
uv run python examples/llm-wiki/setup_demo.py
```

```bash
uv run matrix --runtime scripted --data .matrix-wiki-demo --plugins examples/plugins.json
```

用 `wang@demo.local` / `demo-pass-123` 登录，打开项目「白板定价」：群聊里有整个过程，项目页“Wiki”标签里是编出来的 wiki。

## 用真实模型

```bash
uv run python examples/llm-wiki/setup_demo.py --prepare --fresh
```

```bash
uv run matrix --runtime dsh --data .matrix-wiki-demo --plugins examples/plugins.json
```

然后在项目群聊里依次说 `@知识管家 把项目资料收进 wiki`、`@知识管家 竞品团队版的定价大概在什么区间？用户能接受多少？`，
再在看板上验收任务。2026-10-30 用 dsh 0.2.1-rc.5（默认模型）跑过两遍。第一遍工具还经 `plugin_tools` / `plugin_call` 转一层：

- 收资料：模型先读技能、调 `plugin_tools`，两份资料存成原始资料，编了 3 页（竞品定价、访谈、两者对照的“定价启示”，并标明是 wiki 的归纳）；
  `wiki_lint` 报出它自己算的百分数在原始资料里找不到，它就改回“6 家里 4 家”的原说法，再交付。
- 提问：按推来的目录直接读页面，每个结论都带 `[页面](wiki/…)` 出处，资料没覆盖的明说没有。
- 验收后：插件代表验收的人 @ 它写回；它判断交付说明里没有新事实，只记了一笔日志，没有硬写页面。

第二遍工具按原名出现（`mcp__matrix_net__llm-wiki__add_source` 等），模型不再先查工具列表，直接 `add_source` → `search` → `write` → `lint`
收完两份资料、编了 3 页；提问时直接 `read` 相关页面作答，每个结论带出处。

## 部署方自己运行服务

```bash
WIKI_SECRET=… uv run python examples/plugins/llm-wiki/service/wiki_engine.py --http 8780 --root /srv/wiki --secret-env WIKI_SECRET
```

插件清单里把 `service` 换成 `{"url": "https://<你的地址>/mcp", "tools": {…}}`，Matrix 这边设同一个密钥
`MATRIX_PLUGIN_SECRET_LLM_WIKI=…`。服务按 Matrix 签的项目令牌把数据分到 `/srv/wiki/<项目 id>/`，伪造或缺少令牌一律 401。

## 插件作者自己运行，主持人填地址安装

不经部署方目录（[docs/plugin-install.md](../../docs/plugin-install.md)）：

```bash
echo '{}' > secrets.json
uv run python examples/plugins/llm-wiki/service/wiki_engine.py --http 8781 --root ./wiki-data --secrets secrets.json --package examples/plugins/llm-wiki
```

它在 `http://127.0.0.1:8781/matrix-plugin.zip` 给出插件包（服务地址指回自己）。本机试用时先在管理后台“插件”页把 `127.0.0.1` 加进允许的主机
（http 和内网地址只对列出的主机放行；正式部署用 https 公网地址就不用加）。然后在项目的“插件”页“从地址安装”填这个地址：
确认卡列出插件服务在哪个主机、会收到什么；确认后页面显示一次签名密钥和项目 id，写进 `secrets.json`（`{"<项目 id>": "<密钥>"}`，改了立即生效）。

## 换引擎

插件服务只要是标准 MCP 就行。换成别的 LLM Wiki 引擎（例如 geronimo-iia/llm-wiki 的 Rust 程序），改 `plugin.json` 的
`service` 与 `tools`，技能里的工具名跟着改；挂点改成它暴露的资源 URI，页面不用动。带 `service` 的插件只能放进部署方目录（`plugins.json`）。
