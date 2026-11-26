"""数据库存储（M7，docs/control-plane.md 第 4 节）：一层很薄的 SQL，SQLite（标准库）与 PostgreSQL（psycopg，可选依赖）共用一套表。

不引 ORM。只用最基本的 SQL：`?` 占位符（PostgreSQL 下换成 `%s`）、`INSERT … ON CONFLICT … DO UPDATE`。

表（`SCHEMA`）：

- `meta(key, value)`：`schema_version` 等。
- `entity(kind, id, data, updated_at)`：一行一个对象（Agent、项目、对话的元数据、委托、审批、登录会话……），
  `data` 是现在 `to_dict` 的 JSON，读回来仍经 `store._known` 兼容（丢弃未知字段、缺失取默认）。
- `message(conversation_id, seq, id, data)`：对话消息，按序号只追加；清空对话时整段重写。
- `event(seq, ts, type, data)`：事件总线，序号全局递增且持久，保留 `EVENT_RETAIN_S`。
- `run(id, kind, unit, agent_id, started_at, ended_at, state)`：每一轮运行的开始与结束；重启时还在 `running` 的改成
  `interrupted`（不变量 12：云侧运行中的判失败可续接）。

单写者：同一个库同一时刻只能有一个 Matrix 进程在写（备机与租约是 control-plane.md 的 C3，还没做）。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
EVENT_RETAIN_S = 7 * 24 * 3600

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS entity (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, "
    "updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (kind, id))",
    "CREATE TABLE IF NOT EXISTS message (conversation_id TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL, "
    "data TEXT NOT NULL, PRIMARY KEY (conversation_id, seq))",
    "CREATE TABLE IF NOT EXISTS event (seq BIGINT PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, type TEXT NOT NULL, "
    "data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS event_ts ON event (ts)",
    "CREATE TABLE IF NOT EXISTS run (id TEXT PRIMARY KEY, kind TEXT NOT NULL, unit TEXT NOT NULL, agent_id TEXT, "
    "started_at DOUBLE PRECISION NOT NULL, ended_at DOUBLE PRECISION, state TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS run_state ON run (state)",
]


class DatabaseError(RuntimeError):
    pass


def describe(url: str) -> str:
    """给人看的库地址：去掉密码。"""
    if url.startswith(("postgres://", "postgresql://")):
        u = urlsplit(url)
        host = u.hostname or ""
        return f"postgresql://{u.username + '@' if u.username else ''}{host}{':' + str(u.port) if u.port else ''}{u.path}"
    return url


def resolve_url(spec: str | None, root: Path) -> str | None:
    """`MATRIX_STORE` / `--store` 的写法 → 库地址；None 表示仍用 state.json。

    - 空、`json`：state.json（默认）
    - `sqlite`：`<data>/matrix.db`；`sqlite:///绝对路径`、`sqlite:相对数据目录的路径`
    - `postgresql://用户:密码@主机/库`（密码也可以走 PGPASSWORD 等 libpq 环境变量）
    """
    spec = (spec or "").strip()
    if spec in ("", "json"):
        return None
    if spec == "sqlite":
        return f"sqlite:{root / 'matrix.db'}"
    if spec.startswith("sqlite:"):
        rest = spec.removeprefix("sqlite:")
        rest = rest[2:] if rest.startswith("//") else rest
        p = Path(rest)
        return f"sqlite:{p if p.is_absolute() else root / p}"
    if spec.startswith(("postgres://", "postgresql://")):
        return spec
    raise DatabaseError(f"MATRIX_STORE 写法不认识：{spec!r}（json / sqlite / sqlite:<路径> / postgresql://…）")


class Database:
    """一条连接 + 一把锁。SQLite 的连接允许跨线程用（锁保证同一时刻只有一个线程在用）。"""

    def __init__(self, url: str):
        self.url = url
        self._mu = threading.RLock()
        if url.startswith("sqlite:"):
            self.dialect = "sqlite"
            self.path: Path | None = Path(url.removeprefix("sqlite:"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn: Any = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
            for pragma in ("journal_mode=WAL", "synchronous=FULL", "busy_timeout=30000"):
                self._conn.execute(f"PRAGMA {pragma}")
        else:
            self.dialect = "postgresql"
            self.path = None
            self._conn = self._connect_pg()
        self._depth = 0

    def _connect_pg(self) -> Any:
        try:
            import psycopg  # noqa: PLC0415 —— 可选依赖，只有用 PostgreSQL 时才要
        except ImportError as e:
            raise DatabaseError("用 PostgreSQL 要装 psycopg：uv sync --extra postgres（部署镜像里已带）") from e
        try:
            return psycopg.connect(self.url, autocommit=True)
        except Exception as e:  # noqa: BLE001 —— 连不上时给出去掉密码的地址
            raise DatabaseError(f"连不上数据库 {describe(self.url)}：{e}") from e

    def lost(self) -> bool:
        """PostgreSQL 的连接断了（库重启、切换、网络抖动）。SQLite 没有这回事。"""
        return self.dialect == "postgresql" and bool(self._conn.closed or self._conn.broken)

    def _ensure(self) -> None:
        """调用方持有 _mu。连接断了就重连，不然断一次之后每次保存都失败，只能重启服务（期间的改动全丢）。
        事务中途不重连：那个事务已经没了，由开事务的一方整个重做（DbBackend.save）。"""
        if self._depth == 0 and self.lost():
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = self._connect_pg()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgresql" else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._mu:
            self._ensure()
            self._conn.execute(self._sql(sql), tuple(params))

    def executemany(self, sql: str, rows: list[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._mu:
            self._ensure()
            if self.dialect == "sqlite":
                self._conn.executemany(sql, rows)
            else:
                with self._conn.cursor() as cur:
                    cur.executemany(self._sql(sql), rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._mu:
            self._ensure()
            cur = self._conn.execute(self._sql(sql), tuple(params))
            return list(cur.fetchall())

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """一个事务：要么全部落下，要么都没落。可以嵌套（里面那层不另开事务）。"""
        with self._mu:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._ensure()
            self._depth = 1
            try:
                if self.dialect == "sqlite":
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        yield
                    except BaseException:
                        self._conn.execute("ROLLBACK")
                        raise
                    self._conn.execute("COMMIT")
                else:
                    with self._conn.transaction():
                        yield
            finally:
                self._depth = 0

    def init_schema(self) -> None:
        with self.transaction():
            for stmt in SCHEMA:
                self.execute(stmt)
            got = self.query("SELECT value FROM meta WHERE key = 'schema_version'")
            if not got:
                self.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(got[0][0]) > SCHEMA_VERSION:
                raise DatabaseError(f"库的结构版本是 {got[0][0]}，比这个版本的 Matrix 认识的（{SCHEMA_VERSION}）新："
                                    "换回写入它的那个版本，或从备份恢复")

    def is_empty(self) -> bool:
        return not self.query("SELECT 1 FROM entity LIMIT 1") and not self.query("SELECT 1 FROM message LIMIT 1")

    def size_bytes(self) -> int:
        if self.path is not None:
            return sum(p.stat().st_size for p in (self.path, Path(f"{self.path}-wal")) if p.exists())
        try:
            return int(self.query("SELECT pg_database_size(current_database())")[0][0])
        except Exception:  # noqa: BLE001 —— 没有权限就不报
            return 0

    def backup_to(self, dest: Path) -> None:
        """SQLite 在线备份到一个文件（服务开着也行，备份是一致的快照）。PostgreSQL 用 pg_dump。"""
        if self.dialect != "sqlite":
            raise DatabaseError("PostgreSQL 用 pg_dump 备份（见 docs/reference/storage.md）")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._mu:
            out = sqlite3.connect(dest)
            try:
                self._conn.backup(out)
            finally:
                out.close()

    def close(self) -> None:
        with self._mu:
            with contextlib.suppress(Exception):
                self._conn.close()


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


class EventLog:
    """事件落库：`emit` 只是放进队列，后台线程每 0.2 秒成组提交一次（事件丢最后零点几秒可以接受，实体与消息不行，
    docs/control-plane.md 第 10 节第 3 条）。用自己的一条连接，不和保存状态抢。"""

    def __init__(self, url: str, retain_s: float = EVENT_RETAIN_S):
        self.db = Database(url)
        self.retain_s = retain_s
        self._queue: list[tuple[int, float, str, str]] = []
        self._cv = threading.Condition()
        self._stop = False
        self._last_prune = 0.0
        self._thread = threading.Thread(target=self._loop, name="matrix-events", daemon=True)
        self._thread.start()

    def last_seq(self) -> int:
        got = self.db.query("SELECT MAX(seq) FROM event")
        return int(got[0][0] or 0) if got else 0

    def put(self, seq: int, ts: float, type_: str, data: dict[str, Any]) -> None:
        with self._cv:
            self._queue.append((seq, ts, type_, dumps(data)))
            self._cv.notify()

    def since(self, seq: int, limit: int = 5000) -> list[tuple[int, float, str, dict[str, Any]]]:
        self.flush()
        rows = self.db.query("SELECT seq, ts, type, data FROM event WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit))
        return [(int(s), float(t), str(ty), json.loads(d)) for s, t, ty, d in rows]

    def flush(self) -> None:
        with self._cv:
            batch, self._queue = self._queue, []
        if batch:
            with self.db.transaction():
                self.db.executemany("INSERT INTO event (seq, ts, type, data) VALUES (?, ?, ?, ?) "
                                    "ON CONFLICT (seq) DO NOTHING", batch)
        now = time.time()
        if now - self._last_prune > 3600:
            self._last_prune = now
            self.db.execute("DELETE FROM event WHERE ts < ?", (now - self.retain_s,))

    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._queue and not self._stop:
                    self._cv.wait(0.2)
                stop = self._stop
            try:
                self.flush()
            except Exception as e:  # noqa: BLE001 —— 写事件失败不能拖垮服务；下一批再试
                import sys
                print(f"[matrix] 事件落库失败：{e!r}", file=sys.stderr)
                time.sleep(1)
            if stop:
                return
            time.sleep(0.2)

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout=5)
        with contextlib.suppress(Exception):
            self.flush()
        self.db.close()
