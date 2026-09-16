"""Worktree — WorktreeManager 生命周期 单元测试。

用真实临时 git 仓库验证 create/enter/exit/delete/cleanup/recover。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from core.worktree.manager import WorktreeManager, WorktreeNameError


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """创建含一次提交的真实 git 仓库。"""
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "T")
    (r / "f.txt").write_text("hi")
    _git(r, "add", ".")
    assert _git(r, "commit", "-m", "init").returncode == 0
    return r


@pytest.mark.asyncio
async def test_create_invalid_name(repo: Path):
    mgr = WorktreeManager(repo)
    with pytest.raises(WorktreeNameError):
        await mgr.create("../evil")


@pytest.mark.asyncio
async def test_create_and_enter(repo: Path):
    mgr = WorktreeManager(repo)
    s = await mgr.create("agent-a3f2b1c")
    assert (repo / ".codeforge" / "worktrees" / "agent-a3f2b1c").is_dir()
    assert mgr.store.get("agent-a3f2b1c") is not None
    # 前缀不含斜杠：git 2.55 的 `worktree add` 对含斜杠的分支名一律失败（见 manager.create 注释）
    assert s.branch == "cf-wt-agent-a3f2b1c"

    entered = await mgr.enter("agent-a3f2b1c")
    assert entered.path == s.path


@pytest.mark.asyncio
async def test_create_quick_recovery(repo: Path, monkeypatch):
    """目录已存在 → 快速恢复，不调 git worktree add。"""
    mgr = WorktreeManager(repo)
    s = await mgr.create("agent-a3f2b1c")

    called = {"n": 0}
    orig = mgr._git_worktree_add
    monkeypatch.setattr(
        mgr, "_git_worktree_add", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )

    # 目录已存在 → 不调 git
    s2 = await mgr.create("agent-a3f2b1c")
    assert called["n"] == 0
    assert s2.path == s.path


@pytest.mark.asyncio
async def test_delete_clean(repo: Path):
    mgr = WorktreeManager(repo)
    s = await mgr.create("agent-abcdef1")
    wt_dir = Path(s.path)
    assert wt_dir.exists()

    ok, reason = await mgr.delete("agent-abcdef1")
    assert ok, reason
    assert not wt_dir.exists()
    assert mgr.store.get("agent-abcdef1") is None


@pytest.mark.asyncio
async def test_delete_keeps_dirty(repo: Path):
    mgr = WorktreeManager(repo)
    s = await mgr.create("agent-abcd123")
    wt_dir = Path(s.path)
    (wt_dir / "f.txt").write_text("modified")

    ok, reason = await mgr.delete("agent-abcd123")
    assert not ok
    assert "uncommitted" in reason
    assert wt_dir.exists()  # 保留
    assert mgr.store.get("agent-abcd123") is not None


@pytest.mark.asyncio
async def test_cleanup_all_generated_keeps_user(repo: Path):
    mgr = WorktreeManager(repo)
    o1 = await mgr.create("agent-a111111")  # 生成名，干净 → 会清
    await mgr.create("my-feature", owner="user")  # 用户创建 → 不清

    await mgr.cleanup_all_generated()
    # 用户创建的仍在
    assert mgr.store.get("my-feature") is not None
    # agent- 干净的被清了
    assert mgr.store.get("agent-a111111") is None
    assert not Path(o1.path).exists()


@pytest.mark.asyncio
async def test_recover_all(repo: Path):
    mgr = WorktreeManager(repo)
    await mgr.create("agent-b222222")
    recovered = mgr.recover_all()
    assert any(s.name == "agent-b222222" for s in recovered)
