# 项目档案：项目的上下文不放进代码仓库

> 入口见 [AGENTS.md](../../AGENTS.md)。字段定义见 [domain.md](domain.md)，不变量见 [invariants.md](invariants.md) 第 17 条。

- 纲要（字段名 `brief`）与仍然有效的决定作为背景推送给席位与委托任务（执行者是群成员，档案是项目的公开内容，不违反不变量 9）；只在档案版本变化时重新推送。
  提示词里不写“先读档案”之类的指令（参照 Raft RFC 070：宣布状态重建会让 Agent 先做整理仪式而不是处理工作；research: raft#rfc-070-session-recycle）。
- 档案工具 `record_decision` / `list_decisions` / `publish_file` / `add_link` / `propose_brief` / `propose_skill` 项目成员都有，**不需要 `delegate`**；
  用户任务工具 `open_request` / `deliver_request` / `accept_request` / `reopen_request` 席位都有；其余网络工具（协作工具）需要 `delegate`（`engine.network_tools_for`）。MCP 服务只列出这次能用的工具（`MATRIX_TOOLS`），网络端再按执行上下文检查一次。
- `publish_file` 只能发布当前工作目录里的文件（不含 `project/` 与 `agent-home/`）；`project/` 是只读副本，运行里的改动丢弃。
- `add_link` 把一个 http(s) 链接加进资料（如 Agent 在外部系统里建好的看板）：同一个地址再加是更新名称与说明，`log.jsonl` 记 `ref.add` / `ref.update` 与哪个 Agent，群聊里公开一条；域名对得上连接器目录的，连接器面板认出。人删链接仍只能主持人在项目页上做。
- 资料里地址落在部署方允许嵌入（连接器目录 `embed`）的域名下的链接，主持人可以“设为标签”嵌进项目页（`ref.tab`，见 [connectors-skills.md](connectors-skills.md)“外部页面标签”）。
- 纲要主持人直接改，其他参与者与 Agent 用 `propose_brief` / “建议修改”提建议，主持人采纳或拒绝。纲要超过 14 天没确认，UI 提醒主持人复核。
- 共享项目的档案对所有参与者可见：写进去就等于对所有参与方公开（工具描述与提示词里写明）。
- 项目页 `#project-<id>`（可收藏、刷新、前进后退）：一个项目只有这一页，群聊也在里面。项目列表、侧栏的项目都进这里。
  主区域是面包屑“项目 / 名称” + 对话 / 说明 / 资料 / 技能 / 连接器 / 插件 / 对外标签（加上插件页面和外部页面标签）；
  顶栏的“项目配置”按钮弹出成员、共享项目参与者的编辑框。`#project-<id>/<标签>` 打开某个标签（刷新还在原处），不带标签是“对话”。
  “对话”标签是项目的群聊和自己的项目会话：上面一行切换（群聊、各个会话、新会话），右边是成员与当前对话的操作；
  地址是对话本身 `#conv-<id>`（旧的 `#p-<项目>` 也到这里）。私聊、助理和已归档的对话不属于项目页，在单独的对话视图里。
  说明是纲要（主持人改，别人建议修改）与已定的事；资料是文件与链接，链接认得出服务时标上用哪个连接器打开；技能是项目技能；
  连接器面板见 [connectors-skills.md](connectors-skills.md)。
  资产表列出档案里的文件：名称 / 类型（扩展名）/ 更新人 / 更新时间 / 大小，名称、更新时间、大小可点表头排序，可按类型筛选、按名称搜索；
  点文件名在弹层里预览（Markdown 渲染、CSV 成表、JSON 格式化、图片直接显示，都在页面里转义后渲染，服务端仍按附件下发；
  二进制文件和超过 5 MB 的文本不预览，只给下载），所有成员可下载；
  主持人可上传（多选或拖进列表；任何类型，单个不超过 `FILE_LIMIT` 100 MB，与 Agent 的 `publish_file` 同一个上限；同名覆盖前确认）、删除。
  上传接口 `POST /api/projects/<id>/files?name=<路径>` 的请求体就是文件内容（不用 multipart，同对话附件），字节原样存下；
  也收 JSON `{name, content}`（文本，给脚本用）。插件页面的 `archive.file` 仍只返回文本文件的内容。资料标签下半部分列出链接，可复制位置，主持人增删。
  “更新人”来自 `log.jsonl` 里最近一次 `file.publish`（`project_files` 返回 `by_agent` / `by_user`；移动、删除跟着改），不另存字段。

## 目录层级

- 资料可以分目录：文件名就是档案里的相对路径（`设计/方案.md`），`engine._safe_path` 逐段净化（去掉隐藏前缀、`..`、怪字符），最多 8 层、300 字，
  写不到档案目录以外。目录按需建；同一个位置不能既是文件又是目录。
- 人：主持人可以新建（多级）空目录（`POST /api/projects/<id>/folders`）、移动 / 改名文件或目录（`POST .../moves`，可见范围的设置跟着走）、
  删整个目录（`DELETE .../files/<路径>`）。上传传到当前所在的目录。项目页“资料”里有目录面包屑，表里先列子目录再列文件；
  搜索、按类型筛选时跨全部目录、显示完整路径。`project_detail` 返回 `files`（每个带 `name` 路径与 `access`）与 `folders`（含空目录）。
- Agent：`publish_file` 的 `name` 可以带目录；工作目录的 `project/` 保留同样的层级；档案推送里列的是路径。
- 日志：`folder.create`、`file.move`（`file` → `to`）、`folder.delete`、`file.access`。
- 交付件（发布物）与模板可以带目录里的文件，快照里保留路径；可见范围不随模板带走（模板只是配方，装进去的项目自己设）。

## 可见范围（不变量 26）

- 资料（文件、目录）与项目技能默认所有成员 Agent 都看得到。主持人可以把一项收窄到某些**角色**（项目“说明”里的角色，成员变了范围跟着变）
  和 / 或某些**成员**：`PUT /api/projects/<id>/file-access {path, roles, agents}`、`PUT /api/projects/<id>/skills/<名字>/access {roles, agents}`，
  都空就是改回所有成员可见。存在 `Project.file_access` / `Project.skill_access`。
- 目录的范围管到里面的每个文件：从顶层到文件本身路上的每一条都要满足（`_file_visible`），层层只收窄，文件上的设置放不大目录的。
- 作用在 Agent 拿到的一切：云侧工作目录的 `project/`（`_mount_project_files` 只拷看得到的）与 `skills/`（项目技能层只列看得到的；
  被藏起的项目技能和它自己技能库里的同名技能不冲突，用它自己的）、端侧材料、档案推送里的文件与技能清单（`_archive_text` 按 Agent 算，
  设范围是档案变化，下一轮重新推送）、群聊里说这一项的事件（`Message.about = file:<路径> | skill:<名字>`，按当时的范围过滤，
  事后收窄也一并藏起）。删除、移动受限的内容时，群聊事件不写名字。
- Agent 不能往看不到的位置 `publish_file`（也就覆盖不了看不到的文件），报错让它换个名字或目录。
- 只管 Agent：项目里的人（主持人、参与者）照常都看得到、下载得到，项目页上带锁的标签写着范围；插件页面经桥读档案是以看的人的身份，不过滤。
- 还被范围引用的角色不能删（先改范围）；范围里写的成员退出项目后那一条照样只剩别的人能看（失败时收紧，不放开）。
- 不在此列：看得到的成员经委托、`share_knowledge` 把内容主动转给别人，是显式共享（不变量 9）。
  事件流驱动的刷新只在项目数据或视图状态变化时才重绘项目页。
