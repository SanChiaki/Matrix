# 交付看板（示例）

把“一线各自装查数技能、配 MCP、用本地 Agent 做看板再发布”搬进 Matrix 项目，验证发布与订阅
（[docs/project-publishing.md](../../docs/project-publishing.md)）。

| 面 | 这里怎么做 |
| --- | --- |
| 作业面 | 部署方目录里的“交付数据”连接器（`connectors.json`，自带查数口径技能 `delivery-data-query`）；项目装“交付看板”插件（`plugins/delivery-dashboard`：表 `dashboards`、看板页面、技能 `delivery-dashboard`）。一线人员授权一次自己的交付数据令牌，在群聊里 @ 看板 Agent 做看板 |
| 发布面 | 主持人把技能发布成**技能包**（公开，别的交付项目订阅就能照着做）；把看板文件 + 看板视图发布成**交付件**，读者是区域负责人，开“跟随档案” |
| 节律面 | 插件的定时触发工作日早上叫看板 Agent 刷新；看板文件更新，发布物自动切新版本；读者的反馈回到项目群聊 |

数据权限跟着人走：每个人的 Agent 用自己的授权查数（演示令牌 `demo-huadong` 只能看华东）；区域负责人看到的只是发布出来的快照。

## 文件

- `delivery_data_mcp.py`：演示用的交付数据服务（远程 MCP，Streamable HTTP，只用标准库），代替现有的交付数据系统。
- `connectors.json`：部署方的连接器目录（地址、授权字段、自带技能）。
- `plugins.json`、`plugins/delivery-dashboard/`：部署方的插件目录与插件包。页面同一份代码两用：项目里读“看板”表和档案，发布物里只读快照。
- `setup_demo.py`：用脚本运行时（离线、确定性）搭好一个演示数据目录。

## 看效果

```bash
uv run python examples/delivery-dashboard/setup_demo.py
```

```bash
uv run python examples/delivery-dashboard/delivery_data_mcp.py
```

```bash
uv run matrix --runtime scripted --data .matrix-dashboard-demo --connectors examples/delivery-dashboard/connectors.json --plugins examples/delivery-dashboard/plugins.json
```

打开 http://127.0.0.1:8765 ，密码都是 `demo-pass-123`：

- `zhang@demo.local`（华东一线，项目主持人）：项目「华东交付」→“交付看板”标签看看板，“发布”标签看发出去的两份发布物和读者反馈；
  在群聊里 `@看板 Agent 刷新看板「华东交付周报」` 或 `@看板 Agent 做一个华南交付周报看板`（没有华南的数据权限，会如实说）。
- `li@demo.local`（华东区域负责人，不在项目里）：“发布物”里打开「华东交付周报看板」，看看板、订阅、写反馈；在“助理”里问“华东这周交付情况怎么样？”。
- `wang@demo.local`（华南一线）：“发布物”里能看到公开的“做交付看板的技能包”，看不到华东的看板。

换成真实的数据系统：把 `connectors.json` 里的地址换成现有系统的远程 MCP，授权字段按它的认证方式改；查数口径写进连接器自带的技能。
用 DSH 运行时（去掉 `--runtime scripted`）时，看板 Agent 按技能 `delivery-dashboard` 自己查数、写看板文件。
