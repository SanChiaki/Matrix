# 群组档案：群组的上下文不放进项目仓库

> 入口见 [AGENTS.md](../../AGENTS.md)。字段定义见 [domain.md](domain.md)，不变量见 [invariants.md](invariants.md) 第 17 条。

- Brief 与仍然有效的决定作为背景推送给席位与委托任务（执行者是群成员，档案是群组的公开内容，不违反不变量 9）；只在档案版本变化时重新推送。
  提示词里不写“先读档案”之类的指令（参照 Raft RFC 070：宣布状态重建会让 Agent 先做整理仪式而不是处理工作；research: raft#rfc-070-session-recycle）。
- 档案工具 `record_decision` / `list_decisions` / `publish_file` / `propose_brief` 群组成员都有，**不需要 `delegate`**；
  用户任务工具 `open_request` / `deliver_request` / `accept_request` 席位都有；其余网络工具（协作工具）需要 `delegate`（`engine.network_tools_for`）。MCP 服务只列出这次能用的工具（`AGENTNET_TOOLS`），网络端再按执行上下文检查一次。
- `publish_file` 只能发布当前工作目录里的文件（不含 `group/`、`project/` 与 `agent-home/`）；`group/` 是只读副本，运行里的改动丢弃。
- Brief 主持人直接改，其他参与者与 Agent 用 `propose_brief` / “建议修改”提建议，主持人采纳或拒绝。brief 超过 14 天没确认，UI 提醒主持人复核。
- 联合群组的档案对所有参与者可见：写进去就等于对所有参与方公开（工具描述与提示词里写明）。
