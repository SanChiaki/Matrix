# 蓝图示例

设计见 [docs/blueprints.md](../../docs/blueprints.md)。

- `dev-team/`：研发小队。三个岗位（需求分析、开发、测试），装「队友协作」并把“谁接外部来的新需求”设成岗位“需求分析”，
  GitHub 连接器槽位，纲要、开发约定、技能里用 `{param:product}`、`{role:…}` 占位。
- `setup_demo.py`：部署方把它放进目录，王经理用它建项目「白板研发」，外部回调来一个需求时插件按岗位 @ 需求分析。

```bash
uv run python examples/blueprints/setup_demo.py
```

```bash
uv run matrix --runtime scripted --data .matrix-blueprint-demo --plugins examples/plugins.json
```

登录 `wang@demo.local`（密码 `demo-pass-123`）：左栏“蓝图”里有「研发小队」，可以再用它建一个项目；项目「白板研发」页顶部列着
剩下的手工步骤，“说明”里有岗位，“插件”里能看到按岗位设置的插件，“发布”里能把它再发布成蓝图。管理员 `ops@demo.local` 在
管理后台“插件”页的“蓝图目录”里上传、撤回。
