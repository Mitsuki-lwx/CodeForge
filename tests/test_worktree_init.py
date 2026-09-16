"""Worktree — 环境初始化 单元测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.worktree.init import InitOptions, WorktreeError, initialize


@pytest.mark.asyncio
async def test_init_copies_config(tmp_path: Path):
    root = tmp_path
    wt = root / "wt"
    wt.mkdir()
    (root / "config.yaml").write_text("key: value")

    opts = InitOptions(config_files=["config.yaml"])
    warnings = await initialize(wt, root, opts)
    assert (wt / "config.yaml").read_text() == "key: value"
    assert warnings == []


@pytest.mark.asyncio
async def test_init_recovery_skips_overwrite(tmp_path: Path):
    root = tmp_path
    wt = root / "wt"
    wt.mkdir()
    (root / "config.yaml").write_text("original")
    (wt / "config.yaml").write_text("user-modified")  # 已存在

    opts = InitOptions(config_files=["config.yaml"])
    await initialize(wt, root, opts)
    # 恢复时不覆盖已存在配置
    assert (wt / "config.yaml").read_text() == "user-modified"


@pytest.mark.asyncio
async def test_init_missing_config_skips(tmp_path: Path):
    root = tmp_path
    wt = root / "wt"
    wt.mkdir()
    opts = InitOptions(config_files=["no-such.yaml", ".env-absent"])
    warnings = await initialize(wt, root, opts)
    assert warnings == []


@pytest.mark.asyncio
async def test_init_symlink_best_effort(tmp_path: Path):
    root = tmp_path
    wt = root / "wt"
    wt.mkdir()
    (root / "node_modules").mkdir()  # 大型依赖目录

    opts = InitOptions(symlink_directories=["node_modules"])
    warnings = await initialize(wt, root, opts)
    if (wt / "node_modules").is_symlink():
        assert warnings == []  # 无权限环境下软链失败 → warning；成功则无
    else:
        # best-effort：软链可能因权限失败变成 warning，不应抛错
        assert any("node_modules" in w for w in warnings)


@pytest.mark.asyncio
async def test_init_copy_failure_raises(tmp_path: Path):
    """配置复制失败（目标父路径被文件挡住）应抛错（fail-closed）。"""
    root = tmp_path
    wt = root / "wt"
    wt.mkdir()
    # 源 repo/a/b 存在
    (root / "a").mkdir()
    (root / "a" / "b").write_text("x")
    # wt/a 是文件 → 复制时 mkdir 失败
    (wt / "a").write_text("block")

    opts = InitOptions(config_files=["a/b"])
    with pytest.raises(WorktreeError):
        await initialize(wt, root, opts)
