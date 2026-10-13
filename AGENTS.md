# AGENTS.md

给在本仓库工作的 AI Agent 的项目说明与工程约束。动手前先读完本文件。

项目方向与设计理念见 [CHARTER.md](CHARTER.md)（行动纲领原文）。本文件讲"当前代码怎么做"，
CHARTER 讲"我们要往哪走"。需要判断某个改动是否该做时，以 CHARTER 的方向为准。

## 项目描述

**agentnet**：一个本地运行的 Agent Network（Agent 协作网络）Demo。人与 Agent 持续对话；在群组内对话时，Agent 通过网络工具（MCP）把子任务委托给同组成员，网络负责授权、隔离、追踪与知识沉淀。

- 语言 / 依赖：Python ≥ 3.14，FastAPI + uvicorn，包管理用 `uv`，测试用 pytest。
- 运行时：默认以 DeepSeek Harness（DSH）headless 为 Agent 运行时；`scripted` 运行时是确定性脚本，用于测试与离线演练。
- 状态：全部持久化在 `--data` 目录（默认 `.agentnet/`，已被 gitignore）的 `state.json`；对话、任务各有独立工作目录 `conversations/<id>/`、`tasks/<id>/`。

### 领域模型（`agentnet/models.py`）

| 概念 | 含义 |
| --- | --- |
| Agent（节点） | 名称、描述、能力、**自身权限**（运行时上限）、是否允许人直接对话。系统暂不管理注入 Agent 上下文的经验（Agent Memory） |
| Group（群组） | 只是成员集合。委托只能发给同组成员；当前实现**不会**自动创建 Agent |
| Conversation | 人 ↔ Agent 的长期关系边，续接同一个 DSH 会话 |
| Task（委托） | Agent ↔ Agent 的一次授权：mission / authority / constraints / expected_outcome / share |
| KnowledgeItem | 任务结果沉淀为知识，key 形如 `<task_id>:result` |

权限词表：`web.research`、`file.write`、`shell.exec`、`delegate`。

### 模块地图

| 文件 | 职责 |
| --- | --- |
| `agentnet/engine.py` | `Network` 核心：Agent/群组/对话管理、对话轮次、委托授权与执行、知识可见性、违规检测 |
| `agentnet/protocol.py` | 对话提示词与委托任务提示词构造、委托结果格式化 |
| `agentnet/runtime.py` | `AgentRuntime` 抽象；`DshRuntime`（`--patch` 覆盖层按权限禁用插件 + 沙箱 + 挂载 MCP）；`ScriptedRuntime` |
| `agentnet/mcp_server.py` | 仅标准库的 stdio MCP 服务，暴露 `list_members` / `delegate` / `share_knowledge`，回调 `/internal/tools` |
| `agentnet/server.py` | FastAPI：REST、SSE 事件流 `/api/events`、静态页面、内部工具端点 |
| `agentnet/store.py` | 持久化与种子数据（5 个种子 Agent + 「AI 产品机会研究组」） |
| `agentnet/events.py` | 进程内事件总线（带 seq，供 SSE 与 CLI 消费） |
| `agentnet/scripted.py` | 脚本运行时的场景处理器 |
| `agentnet/cli.py` | 无 UI 跑一轮对话并打印事件流 |
| `agentnet/static/` | 单页前端（原生 JS） |
| `tests/` | `test_network.py`（引擎不变量）、`test_api.py`（HTTP + MCP 回调） |

### 常用命令

```bash
uv sync                                         # 安装依赖
uv run pytest -q                                # 全部测试（当前 18 个，离线，不需要 dsh）
uv run agentnet --runtime scripted              # 启动服务 http://127.0.0.1:8765（离线）
uv run agentnet                                 # 使用 DSH 运行时（需 PATH 中有 dsh）
uv run python -m agentnet.cli "我要研究一个新的 AI 产品机会。" --runtime scripted
```

## 工程约束

### 一、网络不变量（改代码时必须保持，均有测试覆盖）

1. **权限只收窄不放大**：执行者有效权限 = 执行者自身权限 ∩ 委托授予；授予只能来自委托方的有效权限。
2. **只委托群组成员**：目标不在群组即拒绝（`REJECTED`），当前不自动创建 Agent。
   注意：CHARTER 第六节第 4 步是"寻找或创建相关 Agent"，这里与纲领方向存在差距。
   不要擅自引入自动创建；需要时先与人确认设计（创建的 Agent 的权限上限、归属群组、生命周期）。
3. **约束沿委托链累加**，子任务继承全部上游约束。
4. **防环与限深**：目标已在委托链上则拒绝；深度达到 `max_depth`（默认 3）时移除 `delegate`、不挂载网络工具；单个执行上下文最多 `max_delegations`（默认 8）次委托。
5. **协作只走网络**：DSH 内的 subagent / subagent_fork / workflow / goal 等插件始终禁用；调用即记为违规。
6. **上下文默认隔离**：每个委托任务独立会话、独立工作目录；对话续接同一会话。
7. **知识只显式共享**：结果只对产生者与直接委托方可见，其余必须通过 `share` 或 `share_knowledge`，且只能共享自己可见的知识。
8. **身份由网络认定**：调用方由运行期间才有效的执行上下文令牌识别，不信任模型自报身份。
9. **越权必留痕**：工具调用与权限不符时写入 `task.violations` 并发出 `authority.violation` 事件。
10. **重启语义**：重启后进行中的对话回到 `idle`，运行中的任务标记为 `FAILED`。

### 二、工作方式

1. 先读后改：改引擎前读 `engine.py` 与相关测试；保持现有风格（dataclass、类型标注、中文 docstring 与提示词、小工具函数）。
2. 新行为或修 bug 先补测试，用 `ScriptedRuntime` 写确定性测试，不依赖真实 DSH 或网络。
3. 每次改动后运行 `uv run pytest -q`，全部通过再交付。
4. 新增权限时同步改三处：`models.AUTHORITIES`、`models.TOOL_AUTHORITY`、`runtime.AUTHORITY_PLUGINS`。
5. 新增网络工具时同步改：`mcp_server.TOOLS`、`Network.handle_tool`、`protocol._network_tools` 提示词。
6. 状态结构有变化时保证 `Store._load` 能读旧的 `state.json`。
7. 不要提交 `.agentnet/` 下的运行数据；那里可能包含真实研究结果与会话记录。
8. 依赖使用精确或下限版本，经 `uv add` 写入 `pyproject.toml` 与 `uv.lock`。

### 三、安全边界

- 服务只监听 `127.0.0.1`，管理 API **没有鉴权**，这是本地 Demo，不要暴露到公网或改成 `0.0.0.0`。
- `/internal/tools` 只能凭有效执行上下文令牌调用；不要放宽这一校验。
- 没有 `file.write` 的 Agent 运行在只读沙箱；有则只能写自己的工作目录。不要绕过 `build_patch_yaml` 生成的限制。
