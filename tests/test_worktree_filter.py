"""Worktree — 三层过滤 单元测试。"""

from __future__ import annotations

from pathlib import Path

from core.worktree.filter import can_auto_clean


def test_user_created_never_cleaned(tmp_path: Path):
    root = tmp_path
    wt_base = root / ".codeforge" / "worktrees"
    wt_base.mkdir(parents=True)
    d = wt_base / "my-feature"
    d.mkdir()
    r = can_auto_clean(root, "my-feature", str(d))
    assert not r.ok
    assert "user-created" in r.reason


def test_main_dir_never_cleaned(tmp_path: Path):
    r = can_auto_clean(tmp_path, "agent-abc1234", str(tmp_path))
    assert not r.ok
    assert "main working directory" in r.reason


def test_path_escaping_refused(tmp_path: Path):
    r = can_auto_clean(tmp_path, "agent-abc1234", str(tmp_path / "outside"))
    assert not r.ok


def test_missing_dir_refused(tmp_path: Path):
    wt_base = tmp_path / ".codeforge" / "worktrees"
    wt_base.mkdir(parents=True)
    d = wt_base / "agent-abc1234"  # 不存在
    r = can_auto_clean(tmp_path, "agent-abc1234", str(d))
    assert not r.ok


def test_clean_generated_ok(tmp_path: Path):
    wt_base = tmp_path / ".codeforge" / "worktrees"
    wt_base.mkdir(parents=True)
    d = wt_base / "agent-a3f2b1c"
    d.mkdir()
    r = can_auto_clean(tmp_path, "agent-a3f2b1c", str(d))
    assert r.ok
