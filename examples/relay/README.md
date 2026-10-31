# 接力分派（示例）

一个协作方式做成的插件：群聊里没 @ 人的问题，按成员的能力分派给一个人认领；成员做完自己这一环，在回复里写“需要 X”“交给 X”，
插件请 X 看一下。它是 `route` 挂点（[设计](../../docs/plugin-hooks.md) 第 8 节、不变量 24）的验证场景：
**插件只给建议，唤醒由网络按规则执行**。

| 部件 | 在哪 | 做什么 |
| --- | --- | --- |
| 插件服务 | `../plugins/relay/service/relay_engine.py` | 只用标准库的 MCP 服务，不调模型、不记状态。`route_suggest`：人发的、没 @ 人的消息，按成员能力关键词在消息里出现的次数选一个认领人；消息里写了“需要 / 交给 / 请 X”（X 是成员名字或能力），请 X 看一下 |
| `route` 挂点 | `../plugins/relay/plugin.json` | 群聊里有新消息时调 `route_suggest`，可以建议 `claim`（认领）与 `wake`（叫来看）。工具分级是 `hook`：只给挂点用，Agent 看不到 |
| 技能 | `../plugins/relay/skills/relay-handoff/` | 被分派认领就登记任务；被叫来看就做自己这一环；回复最后写“需要<下一环>”；部署这类高风险的一环等人确认 |
| 演示 | `setup_demo.py` | 项目「登录页」，五个成员：反馈、方案、编码、审查、部署，各自的能力是插件分派的依据 |

网络执行建议时的四条规则：

1. 一条人发的消息引出的接力，最多自动唤醒 3 次（认领、叫来看都算），用完了在群聊里说一声，要人 @ 接着推。
2. 这一串里已经出现过的成员不会再被插件叫。
3. 只看项目群聊，项目会话、私聊里的消息不送给插件。
4. 认领只针对人发的、没 @ 人的消息，每条只能一次；认领的那个成员把它登记成任务时，下发人是发消息的人；被叫来看的不能登记。

## 看效果

```bash
uv run python examples/relay/setup_demo.py
```

```bash
uv run matrix --runtime scripted --data .matrix-relay-demo --plugins examples/plugins.json
```

用 `wang@demo.local` / `demo-pass-123` 登录，打开项目「登录页」的群聊：

1. 王经理发“登录页在 Safari 上白屏，用户进不去，帮忙看看”，没 @ 人。插件把它分派给**反馈**认领（消息里有“白屏”），反馈登记任务，下发人是王经理。
2. 反馈复现后说“需要方案”→ 插件请**方案**看一下；方案说“交给编码”→ 请**编码**。
3. 编码说“需要审查”时，这一串的 3 次自动唤醒用完了，群聊里出现“要继续请 @ 成员”。
4. 王经理 @ 审查；审查说“需要部署”→ 插件请**部署**看一下；部署是高风险的一环，等王经理 @ 部署“确认部署”才执行。

## 用真实模型

```bash
uv run python examples/relay/setup_demo.py --prepare --fresh
```

```bash
uv run matrix --runtime dsh --data .matrix-relay-demo --plugins examples/plugins.json
```

然后在群聊里发上面那条不 @ 人的消息，再按群聊里的提示 @ 审查、@ 部署。

## 换成别的协作方式

规则都在插件服务里，网络这一侧不需要知道它是谁：

- Multica 式的任务分派：`route_suggest` 只给 `claim`，按技能和忙闲（`members[].busy`）选人。
- Raft 式的被动投递：对人发的消息给多个 `wake`（最多 3 个会被执行）。
- 改服务的规则不用动 Matrix；`may` 里申请的建议种类在安装时公开给主持人。
