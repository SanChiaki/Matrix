# 队友协作（Raft 式）（示例）

照 Raft 的协作方式做成的插件：群聊就是频道，人发的每条消息都送给频道里的每个队友，不用 @；队友自己判断要不要接，
不是自己的事就沉默。工作项放在任务板上，先认领再动手，认领是先到先得的锁；做完交待验收，人在群聊里说“#N 完成”。
它验证的是 `route` 挂点（[设计](../../docs/plugin-hooks.md) 第 8 节、不变量 24）：一种完整的协作方式可以装进项目，
Matrix 这一侧不需要知道它是谁。

## 和 Raft 的对应

| Raft | 这个插件 |
| --- | --- |
| 被动投递：频道成员收到每条消息都会被唤醒，不需要 @；“@ 不是唤醒已经是成员的 Agent 的那个东西” | route 对人发的每条消息建议叫醒所有频道成员（个人助理除外），先叫被提到的任务负责人、名字或能力被提到的 |
| Agent 只有经 `raft message send` 说的话才发得出去，大多数时候不说话 | 被叫来看的成员只回“（略过）”，网络不发进群聊 |
| 任务：todo / in_progress / in_review / done / closed，转移表 `VALID_TRANSITIONS` | 同一张表，存在插件服务的任务板里 |
| 认领是并发锁：先到先得、单一负责人，失败时说“already assigned to @x” | `task_claim`：别人已认领就失败，提示“已经归 @x” |
| in_review 等人点 done | Agent 最多交到待验收；完成、关闭、重开、退回只认人在群聊里说的“#N 完成 / 关闭 / 重开 / 退回” |
| 身份由 Raft 签发的凭证认定 | 调用者由网络认定，经 MCP 的 `_meta` 交给服务；参数里自报的名字不算 |

刻意不照搬的：

- **一个 Agent 一个会话跨所有频道**：违反 Matrix 的上下文隔离（不变量 7）。每个队友在这个项目里有自己的席位和会话。
- **不限次数的被动唤醒**：Raft 出过同一个 Agent 被唤醒 181 次的事故，事后才加断路器。这里一条人发的消息最多叫醒 3 个，
  同一串里叫过的不再叫；Agent 的发言只叫被点到的人，不再叫醒全频道。插件按网络传来的剩余预算排先后。
- **发言前的新鲜度检查**（Raft 在发消息、认领前检查有没有没读的新消息）：以后再说。
- **MEMORY.md**：Matrix 已经有每个 Agent 的私有目录，起同样的作用，不需要插件来做。

## 部件

| 部件 | 在哪 | 做什么 |
| --- | --- | --- |
| 插件服务 | `../plugins/teammates/service/team_engine.py` | 只用标准库的 MCP 服务，不调模型。任务板存在 `tasks.json`；工具 `tasks`（读）、`task_create`、`task_claim`、`task_update`（写）；`route_suggest`（只给挂点用）负责被动投递和人在群聊里的指令 |
| `route` 挂点 | `../plugins/teammates/plugin.json` | 群聊里有新消息时调 `route_suggest`，只申请 `wake`（叫来看一下） |
| 技能 | `../plugins/teammates/skills/teammate-protocol/` | 怎么当队友：不是自己的事就略过；动手前先认领，认领失败就别做；做完交待验收；完成由人说 |
| 页面 | `../plugins/teammates/page/` | 项目页“任务板”标签：五列看板，每个任务的负责人与记录 |

## 看效果

```bash
uv run python examples/teammates/setup_demo.py
```

```bash
uv run matrix --runtime scripted --data .matrix-team-demo --plugins examples/plugins.json
```

用 `wang@demo.local` / `demo-pass-123` 登录，打开项目「官网」：

1. 王经理说“建任务：登录页加图形验证码”，没 @ 人。任务板上出现 #1，三个队友都收到：后端认领 #1（接口）；
   前端认领不了别人的，自己开了 #2（输入框）并认领；测试判断不是自己的事，略过，群聊里看不到它。
2. 王经理说“下午三点评审一下排期”：三个队友都收到，都略过，群聊里没有多余的回复。
3. 王经理 @ 后端、@ 前端问“做完了吗”：两人把各自的任务交到待验收。
4. 王经理说“#1 完成 #2 完成”：任务板上两个任务完成。

## 用真实模型

```bash
uv run python examples/teammates/setup_demo.py --prepare --fresh
```

```bash
uv run matrix --runtime dsh --data .matrix-team-demo --plugins examples/plugins.json
```

然后在群聊里照上面四句说。被动投递会让每条人发的消息都跑几轮模型，这是 Raft 式协作本来的成本。
