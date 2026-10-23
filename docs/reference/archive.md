# 项目档案：项目的上下文不放进代码仓库

> 入口见 [AGENTS.md](../../AGENTS.md)。字段定义见 [domain.md](domain.md)，不变量见 [invariants.md](invariants.md) 第 17 条。

- 纲要（字段名 `brief`）与仍然有效的决定作为背景推送给席位与委托任务（执行者是群成员，档案是项目的公开内容，不违反不变量 9）；只在档案版本变化时重新推送。
  提示词里不写“先读档案”之类的指令（参照 Raft RFC 070：宣布状态重建会让 Agent 先做整理仪式而不是处理工作；research: raft#rfc-070-session-recycle）。
- 档案工具 `record_decision` / `list_decisions` / `publish_file` / `propose_brief` 项目成员都有，**不需要 `delegate`**；
  用户任务工具 `open_request` / `deliver_request` / `accept_request` 席位都有；其余网络工具（协作工具）需要 `delegate`（`engine.network_tools_for`）。MCP 服务只列出这次能用的工具（`MATRIX_TOOLS`），网络端再按执行上下文检查一次。
- `publish_file` 只能发布当前工作目录里的文件（不含 `project/` 与 `agent-home/`）；`project/` 是只读副本，运行里的改动丢弃。
- 纲要主持人直接改，其他参与者与 Agent 用 `propose_brief` / “建议修改”提建议，主持人采纳或拒绝。纲要超过 14 天没确认，UI 提醒主持人复核。
- 联合项目的档案对所有参与者可见：写进去就等于对所有参与方公开（工具描述与提示词里写明）。
- 项目页 `#project-<id>`（可收藏、刷新、前进后退）：项目列表里点项目、群聊 / 项目会话顶栏的“项目页”、“成员与档案”弹出框里“文件”旁的链接都能进。
  主区域是面包屑“项目 / 名称” + “资产”“资源引用”等标签；顶栏的“项目配置”按钮弹出成员、联合项目参与者的编辑框。
  资产表列出档案里的文件：名称 / 类型（扩展名）/ 更新人 / 更新时间 / 大小，名称、更新时间、大小可点表头排序，可按类型筛选、按名称搜索；
  点文件名在弹层里预览（Markdown 渲染、CSV 成表、JSON 格式化、图片直接显示，都在页面里转义后渲染，服务端仍按附件下发），所有成员可下载；
  主持人可上传（多选或拖进列表；只收文本文件、单个不超过 `FILE_LIMIT`，同名覆盖前确认）、删除。资源引用标签列出引用，可复制位置，主持人增删。
  “更新人”来自 `log.jsonl` 里最近一次 `file.publish`（`project_files` 返回 `by_agent` / `by_user`），不另存字段；文件是平铺的，没有文件夹。
  事件流驱动的刷新只在项目数据或视图状态变化时才重绘项目页。
