# 存储：状态文件、指标

> 入口见 [AGENTS.md](../../AGENTS.md)。代码：`matrix/store.py`（`Store`、`write_durable`、`TimedRLock`、`Timings`）、
> `server.py` 的 `/api/admin/storage` 与 `/metrics`。测试：`tests/test_store_durability.py`。
> 生产级的方向见 [control-plane.md](../control-plane.md) 第 4 节；这里只写已经实现的。

## state.json 怎么写（M2：写入止血）

全部状态仍在内存的对象图里，`Store.save()` 每次把它整份写成 `<data>/state.json`。写法（`store.write_durable`）：

1. 写 `state.json.tmp`，`fsync`；
2. 把现在的 `state.json` 留成 `state.json.bak`（硬链接，任何时刻 `state.json` 都在；文件系统不支持硬链接时退成改名）；
3. `state.json.tmp` 改名成 `state.json`，再 `fsync` 所在目录。

所以断电、`kill -9`、磁盘写满之后，`state.json` 要么是这一次的完整版本，要么是上一次的；不会是空文件或半个文件。

启动时（`Store._read_source`）：

| 情况 | 做法 |
| --- | --- |
| `state.json` 读得了 | 用它 |
| `state.json` 读不了（空、半截、不是 JSON），有 `.bak` | 坏的那份改名为 `state.json.corrupt-<时间>` 留给人看，从 `.bak` 启动，标准错误里报警（最后一次保存的改动可能丢了） |
| 没有 `state.json`，有 `.bak`（改名到一半被打断） | 从 `.bak` 启动并报警 |
| `state.json` 读不了，也没有 `.bak` | 拒绝启动，什么都不动，提示从备份恢复 |

`Store.loaded_from` 记着这次是从哪份读的，管理后台的存储指标里能看到。

`.bak` 与 `.corrupt-*` 都在数据卷里，和 `state.json` 一样敏感，一起备份、一起保护。

## 指标

两个指标只有数字，不含任何内容：

- **保存耗时**（`save`）：每次 `Store.save()` 在锁内花的时间。超过 1 秒（`store.SLOW_SAVE_S`）时标准错误里提醒一行。
- **等锁时间**（`lock_wait`）：引擎与存储共用一把锁（`TimedRLock`），任何地方拿这把锁时真的等了多久（没有争用时不计）。
  保存期间全网停住，这个数字就是“停住了多久”。

每项给次数、总和、最大、最近一次和最近 1000 次的 p50 / p99；只在内存里，重启清零。

- 管理员：`GET /api/admin/storage`（JSON，另有后端、文件大小、`loaded_from`）。不是管理员按不存在处理。
- 抓取程序：`GET /metrics`（Prometheus 文本格式）。管理员登录着能看；部署方在 `.env` 里设了 `MATRIX_METRICS_TOKEN`
  时，带 `Authorization: Bearer <它>` 也能看。两样都没有一律 404。
