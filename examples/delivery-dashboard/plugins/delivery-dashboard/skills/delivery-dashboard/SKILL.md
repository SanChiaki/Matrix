---
name: delivery-dashboard
description: 做交付看板：用交付数据连接器查数，生成看板文件（JSON）发布进项目档案，并在“看板”表里登记。人让你做、刷新交付看板时用。
---

# 做交付看板

## 什么时候用

- 群聊里有人 @ 你做一个交付看板（周报、区域看板、风险看板），或者让你刷新已有的看板。
- 插件的定时触发每天叫你刷新“刷新”是每天的看板。

## 做法

1. 这是有交付目标的事：先 `open_request` 登记（刷新已有看板不用登记）。
2. 用交付数据连接器查数（工具以 `mcp__connectors__delivery-data__` 开头），口径见连接器自带的 `delivery-data-query` 技能：
   - 总览用 `delivery_summary`，范围按人说的区域填 `region`；没说就不填（你的所有者能看的全部）。
   - 没权限的区域会报错：如实告诉对方“我的所有者没有这个区域的数据权限”，不要换别的办法绕过去。
3. 按下面的格式写一个看板文件，文件名用 `<范围拼音或英文>-weekly.json`（如 `huadong-weekly.json`），`publish_file` 发布进档案。
4. 用 `plugin_write` 在 `delivery-dashboard` 插件的 `dashboards` 表里登记或更新一条：标题、看板文件、范围、状态（新的写“草稿”）、
   负责成员（你自己）、刷新（人没说就写“手动”）。刷新时只更新文件，不新增记录。
5. 在回复里用两三句话说结论（按期率、延期最多的项目），写明数据截至哪天；`deliver_request` 交付。
6. 你不能发布给项目外的人。看板要给区域负责人看，请主持人在项目页“发布”里发布成事实，带上这个看板。

## 看板文件格式

```json
{
  "title": "华东交付周报",
  "scope": "华东",
  "as_of": "2026-10-28",
  "source": "交付数据 · delivery_summary",
  "kpis": [
    {"label": "在建项目", "value": 7, "format": "int"},
    {"label": "按期率", "value": 0.71, "format": "pct", "delta": 0.03, "delta_label": "较上周", "good": "up"}
  ],
  "charts": [
    {"type": "line", "title": "近 8 周按期率", "format": "pct", "points": [{"x": "W37", "y": 0.78}]},
    {"type": "bar", "title": "各阶段项目数", "format": "int", "bars": [{"label": "实施", "value": 3}]}
  ],
  "tables": [
    {"title": "延期风险项目",
     "columns": [{"key": "name", "label": "项目"}, {"key": "delay_days", "label": "延期（天）", "align": "right"},
                 {"key": "health", "label": "健康度", "kind": "health"}],
     "rows": [{"name": "…", "delay_days": 12, "health": "红"}]}
  ],
  "notes": "口径说明（一两句）"
}
```

- `format`：`int` 整数，`pct` 0–1 的比例，`days` 天数，`wan` 万元。
- 健康度只用 红 / 黄 / 绿（页面会配图标和文字）。
- 不要把数字写进 `title`；页面会自己显示截至日期。
