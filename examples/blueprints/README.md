# 蓝图示例

设计见 [docs/blueprints.md](../../docs/blueprints.md)。

- `dev-team/`：研发小队。五个岗位（反馈、方案、编码、审查、部署）按 Raft 的方式协作：装「队友协作」并把“谁接外部来的新需求”
  设成岗位“反馈”，GitHub 连接器槽位，纲要、研发流程、技能里用 `{param:product}`、`{role:…}` 占位；人只在方案定稿、合并、上线时说“#N 完成”。
- `setup_demo.py`：部署方把它放进目录，王经理用它建项目「白板研发」，外部回调来一个问题时插件按岗位 @ 反馈。

```bash
uv run python examples/blueprints/setup_demo.py
```

```bash
uv run matrix --runtime scripted --data .matrix-blueprint-demo --plugins examples/plugins.json
```

登录 `wang@demo.local`（密码 `demo-pass-123`）：左栏“蓝图”里有「研发小队」，可以再用它建一个项目；项目「白板研发」页顶部列着
剩下的手工步骤，“说明”里有岗位，“插件”里能看到按岗位设置的插件，“发布”里能把它再发布成蓝图。管理员 `ops@demo.local` 在
管理后台“插件”页的“蓝图目录”里上传、撤回。

## 用真实模型实测（2026-10-31，dsh 0.2.1-rc.5，默认模型）

```bash
uv run python examples/blueprints/setup_demo.py --prepare --fresh
```

```bash
uv run matrix --runtime dsh --data .matrix-blueprint-demo --plugins examples/plugins.json
```

然后往外部回调地址（`.matrix-blueprint-demo/hook-token.txt`）POST 一个 issue：`{"title": "导出 PDF 时中文字体丢失，变成方框", "url": "…"}`，
再在群聊里以人的身份依次说“#1 完成”“#2 完成”“#3 完成”。

| 步骤 | 结果 |
| --- | --- |
| 外部回调 → 反馈 | 插件按岗位 @ 反馈，其余三个岗位被叫来看一眼、都略过。**踩坑**：反馈只有 `web.research`，插件的写工具要 `file.write`，任务建不出来 → 蓝图 2.0.1 给所有岗位 `file.write`，确认卡上提示缺写权限的岗位 |
| 反馈建任务 | #1（现象、初判复现、影响、优先级 P1），交到待验收；给方案建 #2 并 @ 方案 |
| 人说“#1 完成” | **踩坑**：方案在这一串里被叫过，@ 它不再叫醒，交棒卡住 →「队友协作」0.2.1：人说“#N 完成”时叫引用了 #N 的后续任务的人（没人认领的按标题前缀找岗位）。之后方案自动接上，写了方案文件（根因 R1–R5、修复两层、接口变化、风险与回滚）、记了一条项目决定 |
| 人说“#2 完成” | 方案建 #3 交给编码；编码交付补丁包、建 #4 交给审查。**踩坑**：方案回复里顺口提到已完成的 #1，插件把反馈也叫了一次，占掉接力预算，审查没被叫醒 → 0.2.2：Agent 提到已完成的任务不再叫它的负责人 |
| 人说“#3 完成” | 审查自动接上，给出“不通过、退回”：新加的可选参数被静默忽略（接口语义不一致）、验收矩阵缺项；建了返工任务 #5。**踩坑**：代码打成了 zip，审查没有执行权限读不了 → 约定改为按文本文件逐个发布 |

还没走到的：返工回到编码（编码在这一串里已被叫过，按 route 的“同一串里不再叫醒”规则不会自动回来，要人说一句）、合并、部署。

