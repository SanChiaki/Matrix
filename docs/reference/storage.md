# 存储：状态文件、数据库、文件、指标

> 入口见 [AGENTS.md](../../AGENTS.md)。代码：`matrix/store.py`（`Store`、`write_durable`、`TimedRLock`、`Timings`、
> `JsonBackend` / `DbBackend`、`reconcile`）、`matrix/db.py`（SQL 薄层、`EventLog`）、`matrix/storage_cli.py`（迁移命令）、
> `matrix/blobs.py`（内容存储：本机目录 / S3 兼容、暂存区）、`engine.py`「对话附件」一节、
> `server.py` 的 `/api/admin/storage` 与 `/metrics`。测试：`tests/test_store_durability.py`、`tests/test_db_store.py`、
> `tests/test_blobs.py`、`tests/test_blob_attachments.py`。
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

## 数据库存储（M7）

`MATRIX_STORE`（或 `matrix --store`）选状态存在哪：

| 值 | 存在哪 |
| --- | --- |
| 不设、`json` | `<data>/state.json`（默认，和以前一样） |
| `sqlite` | `<data>/matrix.db`（WAL，`synchronous=FULL`） |
| `sqlite:<路径>` | 那个文件（相对路径相对数据目录） |
| `postgresql://用户:密码@主机:端口/库` | PostgreSQL（部署镜像里已带 psycopg；源码运行要 `uv sync --extra postgres`） |

**对象图仍全量在内存里**（引擎靠一把锁保证跨对象的不变量，docs/control-plane.md 第 9 节），换的只是“保存”：

- 实体（Agent、项目、对话元数据、委托、审批、登录会话……，`store.ENTITY_KINDS` / `DICT_KINDS`）一行一个，`entity(kind, id, data)`；
  每次保存序列化、算哈希，只写变了的，删掉的删行。新增一类状态时要加进这两张表，`test_db_kinds_cover_snapshot` 会变红。
- 消息单独成表 `message(conversation_id, seq, id, data)`，只追加；不序列化没变的：每条消息带改动计数（`Message._rev`），
  只重写计数变了的，没人改过旧消息时连逐条比较都省了（`models.MESSAGE_EDITS`）。所以**改消息一律整体赋值**
  （`m.claimed_by = …`），不要原地改它的列表。对话的消息列表被整体换掉（清空对话）时整段重写。
- 一次保存一个事务；没变就不开事务。实测 10 万条消息时增量保存约 6 毫秒（state.json 要 4 秒以上）、启动加载约 5 秒。
- 事件 `event(seq, ts, type, data)`：后台线程每 0.2 秒成组提交（丢最后零点几秒可以接受），保留 7 天；
  序号接着库里的往下数，重启后网页凭 `Last-Event-ID` 续上，内存里没有的从库里补（`EventBus.attach`）。
- 轮次 `run(id, kind, unit, agent_id, started_at, ended_at, state)`：每一轮开始、结束各记一笔；重启时还在 `running` 的
  改成 `interrupted`（不变量 12 照旧：云侧运行中的判失败可续接），数量见指标 `interrupted_runs_at_start`。
  这是“重启不打断”（control-plane.md 第 5 节）的前提，两段式轮次还没做。
- 结构版本在 `meta.schema_version`；库比代码新时拒绝启动。
- **单写者**：同一个库同一时刻只能有一个 Matrix 进程在写。主备、租约（control-plane.md C3）还没做，不要让两个进程连同一个库。

### 从 state.json 迁过来、迁回去

| 情况 | 启动时 |
| --- | --- |
| 库是空的，数据目录里有 `state.json`（或 `.bak`） | 整份导入（走 `Store._load`，旧数据兼容照用），打印对账，`state.json` / `.bak` 改名为 `*.imported` |
| 库是空的，也没有 `state.json` | 全新 |
| 库里有数据，没有 `state.json` | 从库里读 |
| 库里有数据，又有 `state.json` | 拒绝启动：不知道用哪份（比如回退期间又写过），由人决定 |
| `MATRIX_STORE=json`，只有 `state.json.imported` | 拒绝启动：数据在库里，先 `export-state` |

登录会话导入后照旧有效（键本来就是令牌的哈希），没人被登出。

命令（服务停着跑；`--data` / `--store` 默认读 `MATRIX_DATA` / `MATRIX_STORE`）：

```bash
matrix migrate state              # 手动导入并打印对账（每类实体数、每个对话的消息数）；不一致时退出码 1
matrix migrate state --replace    # 库里已有数据时先清空再从 state.json 导入（回退后又要升级）
matrix migrate verify             # 库里的状态和 state.json.imported 按内容比对（都经 Store._load 读一遍再比）
matrix export-state [--out F]     # 库 → state.json，旧版本直接能读（回退前做）；已有时要 --force
matrix backup-db --out F          # SQLite 在线备份（服务开着也行，一致的快照）；PostgreSQL 用 pg_dump
```

回退到不认识数据库的旧版本：停服务 → `matrix export-state` → 去掉 `MATRIX_STORE`（或换回旧镜像）→ 启动。
旧版本认不出哈希过的登录，所有人要重新登录一次。

### 备份

- SQLite：`matrix backup-db --out /data/backup/matrix-<日期>.db`（在线、一致）；或停服务后直接拷 `matrix.db`（连同 `-wal`）。
  要连续备份就用 Litestream 把它流式复制到对象存储。
- PostgreSQL：`pg_dump` 定时全量 + WAL 归档做按时间点恢复。
- 数据库只是状态；附件、档案、私有目录等文件仍在数据卷里，照旧一起备份。先备库、再备文件。

## 文件：附件进内容存储（M8）

`MATRIX_BLOBS` 选对话附件的字节放哪：

| 值 | 放哪 |
| --- | --- |
| 不设、`legacy` | 照旧：每个对话一个目录 `<data>/attachments/<对话>/<sha>`（默认） |
| `local` | 全站去重的内容存储 `<data>/blobs/ab/cd/<sha>`；`local:<绝对路径>` 放别处 |
| `s3://<桶>/<前缀>` | S3 兼容的对象存储；另设 `MATRIX_S3_ENDPOINT`、`MATRIX_S3_ACCESS_KEY`、`MATRIX_S3_SECRET_KEY`、`MATRIX_S3_REGION`（默认 `us-east-1`）。路径风格寻址，SigV4 签名（`tests/test_blobs.py` 用 botocore 算的向量核对） |

规则：

- **全站去重，但只认本对话上传过的**：“这个对话里上传过这份内容”记在状态的 `attachment_refs`（对话 → sha → 上传时间、谁、大小）。
  消息上挂附件、下载、挂进工作目录都先查它；知道别人文件的 sha 也挂不到自己的消息上（`test_dedup_does_not_leak_across_conversations`）。
- **先写字节，再写库**：上传先写进暂存区 `<data>/tmp/uploads/`（fsync），放进内容存储（本机：fsync 后改名；S3：PUT 带内容的 sha256），
  成功了才记引用、保存状态。中途失败最多留下没人引用的块。暂存区用完就删，超过 24 小时的残留启动时与每小时删一次。
- **双读**：读的时候先看内容存储（有引用时），再看旧目录。切到 `local` / `s3` 之前上传的附件不用搬也照样能读；
  从 `local` 切回 `legacy` 后，`blobs/` 里的照样读得到。
- **下载经 Matrix**：可见性检查（看不到就 404）、“一律以附件下载”不变。用对象存储时 Matrix 先拉到本机读缓存
  `<data>/cache/blobs/`（按 sha 校验，对象被改过就不认），再交给浏览器；对象存储本身不对外暴露、不发预签名地址。缓存可以随时删。
- **删除**：清空、删除对话时引用立即去掉；字节等没有任何引用、且放进来超过 7 天（`blobs.GC_GRACE_S`）才由每小时的 `gc_blobs` 删。
- **配额**：`MATRIX_QUOTA_USER_MB`（每人附件总量；同一份内容在一个人名下只算一次，Agent 发的算它的所有者）、
  `MATRIX_QUOTA_TOTAL_GB`（全站）。不设就不限。超了上传被拒并说明已用多少。管理员在 `/api/admin/storage` 的 `files` 里看总量与用得最多的人
  （只有邮箱与字节数，没有文件名与内容）。单个附件 20 MB 的上限不变。

搬迁命令（服务停着跑；可重复、可中断）：

```bash
MATRIX_BLOBS=local matrix migrate blobs            # attachments/<对话>/<sha> → 内容存储，重算 sha 校验、记引用；旧目录不动
matrix migrate blobs --purge-legacy [--yes]        # 删掉已经搬走的旧副本（不带 --yes 只列出）；“只在旧路径”为 0 后再做
matrix migrate blobs --to-legacy                   # 回退：把内容存储里的拷回每个对话的旧目录，旧版本照样读
```

换到对象存储：先 `local` 跑稳 → 用对象存储自己的同步工具（`rclone sync`、`aws s3 sync` 等）把 `<data>/blobs/` 原样同步到
`s3://<桶>/<前缀>/`（对象键和本机同一个布局 `ab/cd/<sha>`）→ 停服务、切配置、`matrix migrate blobs` 补一遍（已在的跳过）→ 启动。

**还没搬的**：发布物快照（`releases/<id>/v<N>/`）与模板版本（`blueprints/<id>/v<N>/`）仍按版本存在数据卷里（各只留最近几版，量不大）；
项目档案、私有目录、插件数据要三方合并与文件系统语义，按设计不进内容存储；沙箱方式的检查点本来就是按 sha 的 `content/`。
