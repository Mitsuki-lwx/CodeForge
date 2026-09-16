"""run 状态存储（SQLite）。

一个 run = 一次「会话执行」：新建会话，或对既有会话的一次恢复/继续。同一个
session 可以有多个 run（每次 `attach --continue` 都开一个新 run），所以 run id
与 session id 是两个概念，不可互相顶替——run id 统一带 `run-` 前缀，在
`codeforge runs` 的输出里与 session id 一眼可分。

存储：`<workspace>/.codeforge/host/runs.db`，WAL 模式。选 WAL 是因为 host 进程
随时可能被 kill -9：WAL 下已提交事务不会因崩溃丢失，且重启后无需 repair。

几个刻意的取舍：

- stdlib `sqlite3`，不引 ORM。一张表、五个查询，ORM 只会让状态迁移更难读。
- 每次操作新开连接、用完即关（`_connect()`），不长期持有。host 的状态迁移是
  低频写（每个 run 几次），开连接的成本可忽略；换来的是 `codeforge runs` 这类
  跨进程读取方永远读到已提交数据，不必处理连接失效或陈旧快照。
- 时间戳一律 Unix 秒（REAL）。跨进程、跨平台、无时区歧义，排序直接可用。
- 本模块只做持久化，**不做状态机校验**：非法迁移（如 `completed` → `running`）
  由 `core/host/lifecycle.py` 拒绝。存储层替调用方把关，会让「谁在何时改了
  什么」变得难以追查。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

# 默认库文件名（位于 <workspace>/.codeforge/host/ 下）
RUNS_DB_FILENAME = "runs.db"

# 未显式传入 pid 时用这个哨兵区分「没传」与「显式传 None」
_UNSET: Any = object()


class RunStatus(str, Enum):
    """run 生命周期状态。

    `queued` → `running` →（`completed` | `failed` | `interrupted`）。
    `interrupted` 表示进程非正常结束（崩溃 / 被杀 / 信号），是恢复逻辑要处理的
    那一档；`failed` 表示进程正常退出但任务本身失败。
    """

    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


# 终态：不会再迁移
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.INTERRUPTED, RunStatus.COMPLETED, RunStatus.FAILED}
)
# 活跃态：占用会话写权限的状态，单写者锁要拦的就是这些
ACTIVE_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})


class RunNotFoundError(LookupError):
    """指定的 run id 不存在。"""


def new_run_id() -> str:
    """生成 run id：`run-YYYYMMDD-HHMMSS-xxxx`。

    与 session id 同形（本地时间戳 + 4 位十六进制随机后缀，防同秒碰撞），但带
    `run-` 前缀，避免在列表输出里与 session id 混淆。
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 —— 本地时间，与 session id 约定一致
    return f"run-{ts}-{secrets.token_hex(2)}"


@dataclass(frozen=True)
class RunRecord:
    """一条 run 记录（不可变快照）。"""

    id: str
    session_id: str
    workspace: str
    status: RunStatus
    created_at: float
    updated_at: float
    pid: int | None = None
    exit_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        """是否已到终态。"""
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 可序列化字典（供控制通道返回给客户端）。"""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pid": self.pid,
            "exit_reason": self.exit_reason,
        }


def _row_to_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        workspace=row["workspace"],
        status=RunStatus(row["status"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        pid=row["pid"],
        exit_reason=row["exit_reason"],
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    pid         INTEGER,
    exit_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at DESC);
"""


class RunStore:
    """run 状态持久化。

    构造即建库（含父目录）并保证 schema 就绪，之后可反复调用而不必担心初始化；
    失败在构造处直接暴露，而不是推迟到第一次写。

    并发：实例本身无状态，每次调用自建自关连接，因此同一实例可被多线程共享。
    跨进程/跨线程的写争用由 SQLite 处理——WAL 允许多读单写，写冲突在
    `timeout` 内自动重试。host 的写路径是单事件循环，实际不会走到争用分支。
    """

    def __init__(self, workspace: str | Path, *, db_path: str | Path | None = None) -> None:
        self._workspace = str(Path(workspace).resolve())
        self._db_path = (
            Path(db_path)
            if db_path is not None
            else Path(self._workspace) / ".codeforge" / "host" / RUNS_DB_FILENAME
        )
        self._ensure_schema()

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── 内部 ────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        try:
            # WAL 是写进库文件的持久属性，设一次即可，后续连接自动继承。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """开一个短命连接；正常退出提交，异常回滚，无论如何关闭。

        `timeout` 即 busy_timeout：并发写时最多等 5 秒再报
        `database is locked`，避免瞬时争用直接失败。
        """
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 写 ──────────────────────────────────────────────────────

    def create(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        pid: int | None = _UNSET,
        status: RunStatus = RunStatus.QUEUED,
    ) -> RunRecord:
        """新建一条 run 记录。

        Args:
            session_id: 该 run 承载的会话 id。
            run_id: 不传则自动生成（`new_run_id()`）。
            pid: 不传则记录当前进程 pid——run 记录几乎总是由将要执行它的进程
                创建，默认当前 pid 可以避免「忘了传 pid 导致崩溃后无法判定
                stale」这类静默失效。显式传 `None` 表示「pid 未知」。
            status: 初始状态，默认 `queued`；`lifecycle.start()` 再转 `running`。

        Returns:
            落库后的记录快照。
        """
        if not session_id:
            raise ValueError("session_id 不能为空")
        rid = run_id or new_run_id()
        pid_val = os.getpid() if pid is _UNSET else pid
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs"
                " (id, session_id, workspace, status, created_at, updated_at, pid, exit_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (rid, session_id, self._workspace, status.value, now, now, pid_val),
            )
        record = self.get(rid)
        assert record is not None  # 刚插入，读不到说明存储损坏
        return record

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        exit_reason: str | None = None,
        pid: int | None = _UNSET,
    ) -> RunRecord:
        """迁移 run 状态并刷新 `updated_at`。

        不做迁移合法性校验（见模块 docstring）；`exit_reason` 只在本次显式传入时
        覆盖，`pid` 同理。
        """
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, time.time()]
        if exit_reason is not None:
            sets.append("exit_reason = ?")
            params.append(exit_reason)
        if pid is not _UNSET:
            sets.append("pid = ?")
            params.append(pid)
        params.append(run_id)

        with self._connect() as conn:
            cur = conn.execute(
                # sets 是字面量列表拼接，值全部走绑定参数
                f"UPDATE runs SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise RunNotFoundError(f"run 不存在: {run_id}")
        record = self.get(run_id)
        assert record is not None  # 刚更新过
        return record

    # ── 读 ──────────────────────────────────────────────────────

    def get(self, run_id: str) -> RunRecord | None:
        """按 id 取记录；不存在返回 `None`。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_record(row) if row is not None else None

    def list(
        self,
        *,
        limit: int | None = 50,
        status: RunStatus | None = None,
        session_id: str | None = None,
    ) -> list[RunRecord]:
        """列出 run，按 `updated_at` 倒序（最近活跃在前）。

        Args:
            limit: 最多返回条数；`None` 表示不限（供内部扫描用，如 stale 改判）。
            status: 只看某一状态。
            session_id: 只看某个会话的 run（一个会话可有多次执行）。
        """
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, rowid DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def latest_active(self, *, session_id: str | None = None) -> RunRecord | None:
        """最近一个仍处于活跃态（`queued` / `running`）的 run。

        首期只允许单个 active run，控制通道的 `send_message` 隐式作用于它，
        所以这个查询是「当前在跑哪个 run」的唯一入口。
        """
        placeholders = ", ".join("?" * len(ACTIVE_STATUSES))
        params: list[Any] = [s.value for s in ACTIVE_STATUSES]
        # 占位符个数由枚举长度决定（字面量），状态值仍走绑定参数
        sql = f"SELECT * FROM runs WHERE status IN ({placeholders})"
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY updated_at DESC, rowid DESC LIMIT 1"

        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _row_to_record(row) if row is not None else None
