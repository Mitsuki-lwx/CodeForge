"""单写者锁测试。

要点：**跨进程**互斥是真的，不能只在同进程里自说自话。所以关键的几条用
`subprocess` 起真子进程来验：一个子进程持锁时父进程必须被拒；子进程被
`os._exit()`（等价 `kill -9`，不做任何清理）留下的锁必须被识别为崩溃残留并回收。

另外覆盖 Writer 的持锁生命周期：锁跟着写入器生，跟着写入器死。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent.bootstrap import build_session
from core.archive.writer import Writer
from core.host import lock as lock_mod
from core.host.lock import LOCK_FILENAME, SessionLock, SessionLockedError
from core.host.proc import pid_alive

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_child(code: str, session_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, str(session_dir)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )


def _hold_lock_then_die(session_dir: Path) -> int:
    """子进程抢锁后 `os._exit()`（不释放），返回其 pid。

    这是 `kill -9` 的忠实模拟：锁文件留在磁盘上，持有者进程已经不在了。
    """
    code = (
        "import os, sys\n"
        "from core.host.lock import SessionLock\n"
        "SessionLock(sys.argv[1]).acquire()\n"
        "print(os.getpid(), flush=True)\n"
        "os._exit(0)\n"
    )
    proc = _run_child(code, session_dir)
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())


def _spawn_lock_holder(session_dir: Path) -> tuple[subprocess.Popen, int]:
    """起一个**持锁且不退出**的子进程，返回 (Popen, pid)；调用方负责 kill。

    与 `_hold_lock_then_die` 的区别就是它还活着——只有活着的持有者才能测出
    「被拒绝」，用已退出的持有者去测只会测到 stale 回收。
    """
    code = (
        "import os, sys, time\n"
        "from core.host.lock import SessionLock\n"
        "SessionLock(sys.argv[1]).acquire()\n"
        "print(f'READY {os.getpid()}', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(session_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )
    line = proc.stdout.readline().strip()
    if not line.startswith("READY "):
        proc.kill()
        raise AssertionError(f"子进程未就绪: {line!r} / {proc.stderr.read()}")
    return proc, int(line.split()[1])


# ── 基本语义 ────────────────────────────────────────────────────


def test_acquire_writes_pid_and_release_removes(tmp_path):
    lock = SessionLock(tmp_path)
    assert lock.read_holder() is None
    lock.acquire()
    assert lock.held
    assert lock.read_holder() == lock.pid
    lock.release()
    assert not lock.held
    assert not lock.path.exists()


def test_acquire_is_idempotent(tmp_path):
    lock = SessionLock(tmp_path)
    lock.acquire()
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.path.exists()


def test_release_is_idempotent(tmp_path):
    lock = SessionLock(tmp_path)
    lock.acquire()
    lock.release()
    lock.release()
    assert not lock.path.exists()


def test_release_without_acquire_does_not_delete_others_lock(tmp_path):
    """没抢到锁的实例调用 release() 不能把别人的锁删掉。"""
    holder = SessionLock(tmp_path)
    holder.acquire()
    bystander = SessionLock(tmp_path)
    bystander.release()
    assert holder.path.exists()
    assert holder.read_holder() == holder.pid


def test_reacquire_after_release(tmp_path):
    lock = SessionLock(tmp_path)
    lock.acquire()
    lock.release()
    lock.acquire()
    assert lock.held
    lock.release()


def test_context_manager_releases(tmp_path):
    with SessionLock(tmp_path) as lock:
        assert lock.held
    assert not lock.path.exists()


def test_context_manager_releases_on_exception(tmp_path):
    lock = SessionLock(tmp_path)
    with pytest.raises(RuntimeError), lock:
        raise RuntimeError("boom")
    assert not lock.path.exists()


def test_lock_file_lives_in_session_dir(tmp_path):
    assert SessionLock(tmp_path).path == tmp_path / LOCK_FILENAME


# ── 互斥 ────────────────────────────────────────────────────────


def test_same_process_second_lock_rejected(tmp_path):
    """同进程再抢同一个会话也要被拒——pid 活着就是活着。"""
    holder = SessionLock(tmp_path)
    holder.acquire()
    with pytest.raises(SessionLockedError) as exc:
        SessionLock(tmp_path).acquire()
    assert exc.value.holder_pid == holder.pid


def test_other_process_holding_lock_rejects_us(tmp_path):
    """真子进程持锁（且还活着）时父进程必须被拒，错误里带上占用者 pid。"""
    proc, child_pid = _spawn_lock_holder(tmp_path)
    try:
        assert pid_alive(child_pid)
        with pytest.raises(SessionLockedError) as exc:
            SessionLock(tmp_path).acquire()
        assert exc.value.holder_pid == child_pid
        assert str(child_pid) in str(exc.value)
    finally:
        proc.kill()
        proc.wait()


def test_child_process_can_acquire_when_free(tmp_path):
    """反向验证：没人持锁时子进程能拿到（否则上面的"被拒"可能是因为子进程压根不行）。"""
    code = (
        "import sys\n"
        "from core.host.lock import SessionLock\n"
        "SessionLock(sys.argv[1]).acquire()\n"
        "print('ACQUIRED')\n"
    )
    proc = _run_child(code, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ACQUIRED"


def test_child_process_is_rejected_while_parent_holds(tmp_path):
    holder = SessionLock(tmp_path)
    holder.acquire()
    code = (
        "import sys\n"
        "from core.host.lock import SessionLock, SessionLockedError\n"
        "try:\n"
        "    SessionLock(sys.argv[1]).acquire()\n"
        "    print('ACQUIRED')\n"
        "except SessionLockedError:\n"
        "    print('REJECTED')\n"
    )
    proc = _run_child(code, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "REJECTED"


# ── stale 回收 ──────────────────────────────────────────────────


def test_dead_holder_lock_is_reclaimed(tmp_path):
    """崩溃残留（锁文件在、持有者已死）必须能被清掉重抢，否则会话永久卡死。"""
    dead_pid = _hold_lock_then_die(tmp_path)
    assert (tmp_path / LOCK_FILENAME).exists()

    lock = SessionLock(tmp_path)
    lock.acquire()
    assert lock.held
    assert lock.read_holder() == lock.pid
    assert lock.read_holder() != dead_pid
    lock.release()


def test_unparseable_lock_file_is_treated_as_stale(tmp_path):
    """创建后立刻崩溃会留下空锁文件——必须能自愈，不能要求用户手动删。"""
    (tmp_path / LOCK_FILENAME).write_text("", encoding="ascii")
    lock = SessionLock(tmp_path)
    lock.acquire()
    assert lock.held
    lock.release()


def test_garbage_lock_file_is_treated_as_stale(tmp_path):
    (tmp_path / LOCK_FILENAME).write_text("not-a-pid\n", encoding="ascii")
    lock = SessionLock(tmp_path)
    lock.acquire()
    assert lock.held
    lock.release()


def test_zero_pid_lock_file_is_treated_as_stale(tmp_path):
    (tmp_path / LOCK_FILENAME).write_text("0\n", encoding="ascii")
    lock = SessionLock(tmp_path)
    lock.acquire()
    assert lock.held
    lock.release()


# ── Writer 生命周期 ────────────────────────────────────────────


def test_writer_acquires_and_releases_lock(tmp_path):
    lock = SessionLock(tmp_path)
    writer = Writer(tmp_path, lock=lock)
    assert lock.held
    writer.close()
    assert not lock.held
    assert not lock.path.exists()


def test_writer_close_is_idempotent_with_lock(tmp_path):
    lock = SessionLock(tmp_path)
    writer = Writer(tmp_path, lock=lock)
    writer.close()
    writer.close()
    assert not lock.held


def test_second_writer_on_locked_session_is_rejected(tmp_path):
    first = Writer(tmp_path, lock=SessionLock(tmp_path))
    try:
        with pytest.raises(SessionLockedError):
            Writer(tmp_path, lock=SessionLock(tmp_path))
    finally:
        first.close()


def test_writer_without_lock_leaves_no_lock_file(tmp_path):
    """不传锁时不产生锁文件——默认路径零副作用。"""
    writer = Writer(tmp_path)
    try:
        assert not (tmp_path / LOCK_FILENAME).exists()
    finally:
        writer.close()


def test_writer_releases_lock_when_open_fails(tmp_path):
    """开文件失败不能把锁留在盘上（否则后续谁也拿不到）。"""
    lock = SessionLock(tmp_path)
    bad_dir = tmp_path / "as_a_dir"
    bad_dir.mkdir()
    (bad_dir / "conversation.jsonl").mkdir()  # 让 open(...,"a") 失败
    with pytest.raises(OSError):
        Writer(bad_dir, lock=lock)
    assert not lock.held


# ── 与 build_session 的接线 ────────────────────────────────────


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        protocol="openai",
        model="gpt-4o",
        api_key="sk-test-not-a-real-key",
    )


async def test_build_session_locks_when_asked(tmp_path):
    bundle = await build_session(
        provider=_provider(), workspace=tmp_path, lock_session=True
    )
    try:
        assert bundle.lock is not None
        assert bundle.lock.held
        assert bundle.lock.path.exists()
    finally:
        bundle.close()
    assert not (bundle.lock.path).exists()


async def test_build_session_second_lock_rejected(tmp_path):
    """同一会话目录的第二个 host 必须起不来。"""
    first = await build_session(
        provider=_provider(), workspace=tmp_path, lock_session=True
    )
    try:
        target = first.runtime.session.session_dir
        with pytest.raises(SessionLockedError):
            await build_session(
                provider=_provider(),
                workspace=tmp_path,
                session_dir=target,
                conversation=ConversationManager(),
                lock_session=True,
            )
    finally:
        first.close()


async def test_build_session_closes_lock_then_reacquirable(tmp_path):
    first = await build_session(
        provider=_provider(), workspace=tmp_path, lock_session=True
    )
    target = first.runtime.session.session_dir
    first.close()
    second = SessionLock(target)
    second.acquire()
    assert second.held
    second.release()


async def test_build_session_without_lock_creates_no_lock_file(tmp_path):
    """默认（TUI 路径）不加锁：行为与改动前一致。"""
    bundle = await build_session(provider=_provider(), workspace=tmp_path)
    try:
        assert bundle.lock is None
        assert not (Path(bundle.runtime.session.session_dir) / LOCK_FILENAME).exists()
    finally:
        bundle.close()


# ── 平台约束 ────────────────────────────────────────────────────


def test_lock_module_does_not_use_fcntl():
    """锁必须跨平台：Windows 上没有 fcntl。

    只查 import 语句——模块 docstring 里为了说明"不用 fcntl"会提到这个词。
    """
    src = Path(lock_mod.__file__).read_text(encoding="utf-8")
    assert "import fcntl" not in src
    assert "from fcntl" not in src
