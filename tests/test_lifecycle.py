"""run 生命周期状态机测试。

覆盖：四条正常迁移、终态不可复活、非法迁移被拒、stale 改判（存活/已死/queued
窗口/终态不动/pid 未知）、迁移表不变量。
"""

from __future__ import annotations

import os

import pytest

from core.host import lifecycle as lifecycle_mod
from core.host.lifecycle import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    RunLifecycle,
)
from core.host.run_store import (
    TERMINAL_STATUSES,
    RunNotFoundError,
    RunStatus,
    RunStore,
)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path)


@pytest.fixture
def lc(store):
    return RunLifecycle(store)


# ── 正常迁移 ────────────────────────────────────────────────────


def test_start_moves_to_running_and_records_pid(store, lc):
    """queued → running，并把 pid 刷成当前进程。"""
    rec = store.create("s-1", pid=None)
    started = lc.start(rec.id)
    assert started.status is RunStatus.RUNNING
    assert started.pid == os.getpid()


def test_complete_records_reason(store, lc):
    rec = lc.start(store.create("s-1").id)
    done = lc.complete(rec.id, reason="user quit")
    assert done.status is RunStatus.COMPLETED
    assert done.exit_reason == "user quit"
    assert done.is_terminal


def test_fail_records_reason(store, lc):
    rec = lc.start(store.create("s-1").id)
    failed = lc.fail(rec.id, reason="tool crashed")
    assert failed.status is RunStatus.FAILED
    assert failed.exit_reason == "tool crashed"


def test_interrupt_records_reason(store, lc):
    rec = lc.start(store.create("s-1").id)
    killed = lc.interrupt(rec.id, reason="signal 9")
    assert killed.status is RunStatus.INTERRUPTED
    assert killed.exit_reason == "signal 9"


def test_start_unknown_run_raises(lc):
    with pytest.raises(RunNotFoundError):
        lc.start("run-nope")


# ── 非法迁移 ────────────────────────────────────────────────────


def test_terminal_state_cannot_be_revived(store, lc):
    """completed 之后不能重新 start——要接着干就开新 run。"""
    rec = lc.start(store.create("s-1").id)
    lc.complete(rec.id)
    with pytest.raises(IllegalTransitionError) as exc:
        lc.start(rec.id)
    assert exc.value.frm is RunStatus.COMPLETED
    assert exc.value.to is RunStatus.RUNNING


def test_cannot_interrupt_after_complete(store, lc):
    """迟到的信号不能覆盖已经写好的终态。"""
    rec = lc.start(store.create("s-1").id)
    lc.complete(rec.id, reason="graceful")
    with pytest.raises(IllegalTransitionError):
        lc.interrupt(rec.id, reason="late signal")
    assert store.get(rec.id).status is RunStatus.COMPLETED


def test_transition_table_covers_all_statuses():
    """迁移表必须覆盖全部状态，且终态是汇点。"""
    assert set(ALLOWED_TRANSITIONS) == set(RunStatus)
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


# ── stale 改判 ──────────────────────────────────────────────────


def test_mark_stale_keeps_live_run(store, lc):
    """pid 还活着就不许动——把在跑的 run 标成 interrupted 是状态损坏。"""
    rec = lc.start(store.create("s-1").id)
    assert lc.mark_stale_interrupted() == []
    assert store.get(rec.id).status is RunStatus.RUNNING


def test_mark_stale_flags_dead_pid(store, lc, monkeypatch):
    monkeypatch.setattr(lifecycle_mod, "pid_alive", lambda pid: False)
    rec = lc.start(store.create("s-1").id)
    stale = lc.mark_stale_interrupted()
    assert [r.id for r in stale] == [rec.id]
    got = store.get(rec.id)
    assert got is not None
    assert got.status is RunStatus.INTERRUPTED
    assert "已不存在" in got.exit_reason


def test_mark_stale_scans_queued_window_too(store, lc, monkeypatch):
    """queued 是 create→start 之间的窗口，进程在这里挂掉同样要收尾。"""
    monkeypatch.setattr(lifecycle_mod, "pid_alive", lambda pid: False)
    rec = store.create("s-1")  # 停在 queued
    assert [r.id for r in lc.mark_stale_interrupted()] == [rec.id]
    assert store.get(rec.id).status is RunStatus.INTERRUPTED


def test_mark_stale_leaves_terminal_runs_alone(store, lc, monkeypatch):
    rec = lc.start(store.create("s-1").id)
    lc.complete(rec.id, reason="ok")
    monkeypatch.setattr(lifecycle_mod, "pid_alive", lambda pid: False)
    assert lc.mark_stale_interrupted() == []
    assert store.get(rec.id).status is RunStatus.COMPLETED


def test_mark_stale_pid_unknown_is_stale(store, lc):
    """pid=None 表示没有进程可归属，不按存活处理。"""
    rec = store.create("s-1", pid=None)
    assert [r.id for r in lc.mark_stale_interrupted()] == [rec.id]


def test_mark_stale_covers_multiple_active_runs(store, lc, monkeypatch):
    monkeypatch.setattr(lifecycle_mod, "pid_alive", lambda pid: False)
    a = store.create("s-1")
    b = lc.start(store.create("s-2").id)
    assert {r.id for r in lc.mark_stale_interrupted()} == {a.id, b.id}


def test_mark_stale_only_touches_its_own_store(store, lc, tmp_path):
    """库按 workspace 分，别的 workspace 的 run 不受影响。"""
    other = RunStore(tmp_path / "other")
    rec = other.create("s-1", pid=None)
    assert lc.mark_stale_interrupted() == []
    assert other.get(rec.id).status is RunStatus.QUEUED
