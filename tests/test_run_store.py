"""run 状态存储测试。

覆盖：建库与 schema、默认状态与 pid 记录、状态迁移与 updated_at 刷新、
按状态/会话过滤、活跃 run 查询、跨实例持久化、WAL 生效、枚举不变量。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

import pytest

from core.host.run_store import (
    ACTIVE_STATUSES,
    RUNS_DB_FILENAME,
    TERMINAL_STATUSES,
    RunNotFoundError,
    RunStatus,
    RunStore,
    new_run_id,
)

SESSION_ID = "20260916-154000-abcd"


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path)


def _force_updated_at(db_path, run_id: str, ts: float) -> None:
    """直接改库里的 updated_at——用于构造排序测试所需的确定输入。

    只搭状态、不碰被测行为：`update_status` 用的是 `time.time()`，两次调用是否
    落在同一微秒不由测试控制，靠 sleep 去凑时序是脆的。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (ts, run_id))
        conn.commit()
    finally:
        conn.close()


# ── 建库 ────────────────────────────────────────────────────────


def test_db_created_under_workspace(store, tmp_path):
    """构造即在 <workspace>/.codeforge/host/ 下建库。"""
    assert store.db_path == tmp_path / ".codeforge" / "host" / RUNS_DB_FILENAME
    assert store.db_path.is_file()


def test_wal_mode_enabled(store):
    """库文件持久为 WAL——host 被 kill -9 后已提交事务不丢、无需 repair。"""
    conn = sqlite3.connect(store.db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


# ── 创建 ────────────────────────────────────────────────────────


def test_create_defaults(store, tmp_path):
    """默认落到 queued，并记录当前进程 pid（避免崩溃后无法判定 stale）。"""
    rec = store.create(SESSION_ID)
    assert rec.id.startswith("run-")
    assert rec.status is RunStatus.QUEUED
    assert rec.pid == os.getpid()
    assert rec.session_id == SESSION_ID
    assert rec.workspace == str(tmp_path.resolve())
    assert rec.exit_reason is None
    assert rec.created_at == pytest.approx(rec.updated_at)
    assert not rec.is_terminal


def test_create_explicit_pid_none(store):
    """显式 pid=None 表示「pid 未知」，不被默认值覆盖。"""
    assert store.create(SESSION_ID, pid=None).pid is None


def test_create_rejects_empty_session_id(store):
    """空 session_id 是调用方的 bug，就地拒绝而不是落一条无主记录。"""
    with pytest.raises(ValueError):
        store.create("")


def test_run_ids_are_unique_and_prefixed(store):
    """同一会话的两次执行是两个 run，id 必须不同且带 run- 前缀。"""
    a = store.create(SESSION_ID)
    b = store.create(SESSION_ID)
    assert a.id != b.id
    assert a.id.startswith("run-") and b.id.startswith("run-")


def test_create_with_explicit_run_id(store):
    rec = store.create(SESSION_ID, run_id="run-fixed-0001")
    assert store.get("run-fixed-0001") == rec


def test_new_run_id_shape():
    assert re.match(r"^run-\d{8}-\d{6}-[0-9a-f]{4}$", new_run_id())


# ── 状态迁移 ────────────────────────────────────────────────────


def test_update_status_transitions_and_reason(store):
    rec = store.create(SESSION_ID)
    running = store.update_status(rec.id, RunStatus.RUNNING)
    assert running.status is RunStatus.RUNNING
    assert running.exit_reason is None

    done = store.update_status(rec.id, RunStatus.COMPLETED, exit_reason="user quit")
    assert done.status is RunStatus.COMPLETED
    assert done.exit_reason == "user quit"
    assert done.created_at == rec.created_at  # 创建时间不被迁移改写
    assert done.updated_at >= rec.updated_at


def test_update_status_preserves_reason_when_omitted(store):
    """exit_reason 只在显式传入时覆盖——否则后一次迁移会抹掉失败原因。"""
    rec = store.create(SESSION_ID)
    store.update_status(rec.id, RunStatus.INTERRUPTED, exit_reason="killed")
    after = store.update_status(rec.id, RunStatus.FAILED)
    assert after.exit_reason == "killed"


def test_update_status_can_clear_pid(store):
    rec = store.create(SESSION_ID)
    assert rec.pid == os.getpid()
    assert store.update_status(rec.id, RunStatus.INTERRUPTED, pid=None).pid is None


def test_update_status_unknown_run_raises(store):
    """改不存在的 run 必须报错，否则「状态没落库」会被静默吞掉。"""
    with pytest.raises(RunNotFoundError):
        store.update_status("run-nope", RunStatus.RUNNING)


def test_is_terminal(store):
    rec = store.create(SESSION_ID)
    assert not rec.is_terminal
    assert store.update_status(rec.id, RunStatus.RUNNING).is_terminal is False
    assert store.update_status(rec.id, RunStatus.INTERRUPTED).is_terminal is True


# ── 查询 ────────────────────────────────────────────────────────


def test_get_missing_returns_none(store):
    assert store.get("run-nope") is None


def test_list_orders_by_updated_at_desc(store):
    a = store.create(SESSION_ID)
    b = store.create(SESSION_ID)
    _force_updated_at(store.db_path, a.id, 1_000.0)
    _force_updated_at(store.db_path, b.id, 2_000.0)
    assert [r.id for r in store.list()] == [b.id, a.id]


def test_list_filters_by_status_and_session(store):
    a = store.create("s-1")
    b = store.create("s-2")
    store.update_status(b.id, RunStatus.COMPLETED)

    assert [r.id for r in store.list(status=RunStatus.QUEUED)] == [a.id]
    assert [r.id for r in store.list(session_id="s-2")] == [b.id]
    assert len(store.list()) == 2


def test_list_respects_limit(store):
    for i in range(3):
        store.create(f"s-{i}")
    assert len(store.list(limit=2)) == 2


def test_latest_active_picks_active_run(store):
    old = store.create("s-1")
    store.update_status(old.id, RunStatus.COMPLETED)
    new = store.create("s-2")
    active = store.latest_active()
    assert active is not None
    assert active.id == new.id


def test_latest_active_ignores_terminal(store):
    rec = store.create(SESSION_ID)
    store.update_status(rec.id, RunStatus.FAILED, exit_reason="boom")
    assert store.latest_active() is None


def test_latest_active_scoped_by_session(store):
    a = store.create("s-1")
    b = store.create("s-2")
    assert store.latest_active(session_id="s-1").id == a.id
    assert store.latest_active(session_id="s-2").id == b.id


# ── 持久化与序列化 ──────────────────────────────────────────────


def test_state_survives_new_instance(store, tmp_path):
    """跨进程可见性：另一实例（模拟 `codeforge runs`）能读到已提交状态。"""
    rec = store.create(SESSION_ID)
    store.update_status(rec.id, RunStatus.RUNNING)
    got = RunStore(tmp_path).get(rec.id)
    assert got is not None
    assert got.status is RunStatus.RUNNING


def test_record_to_dict_is_json_serializable(store):
    rec = store.create(SESSION_ID)
    payload = json.loads(json.dumps(rec.to_dict()))
    assert payload["status"] == "queued"
    assert payload["session_id"] == SESSION_ID
    assert set(payload) == {
        "id",
        "session_id",
        "workspace",
        "status",
        "created_at",
        "updated_at",
        "pid",
        "exit_reason",
    }


def test_status_sets_partition_all_statuses():
    """活跃态与终态必须不重不漏地覆盖全部状态——stale 改判靠这个划分。"""
    assert ACTIVE_STATUSES & TERMINAL_STATUSES == frozenset()
    assert ACTIVE_STATUSES | TERMINAL_STATUSES == frozenset(RunStatus)
