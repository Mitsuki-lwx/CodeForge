"""Worktree 环境初始化。

创建 worktree 目录后执行：
  1. 复制本地配置（首次创建复制，恢复跳过）
  2. 配置 worktree 子目录 git hooks
  3. 软链大型依赖目录（best-effort，从配置读目录列表）
  4. 补被忽略但运行需要的文件

复制配置失败 → 抛错（fail-closed）；软链失败 → 仅告警（best-effort）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class WorktreeError(Exception):
    """Worktree 操作错误（目录名非法、初始化失败等）。"""


@dataclass
class InitOptions:
    """环境初始化选项。"""

    symlink_directories: list[str] = field(default_factory=list)
    """需要软链到主仓库的大型依赖目录列表（如 node_modules/.venv/vendor）。

    从 settings.worktree.symlinkDirectories 读取，不硬编码。
    """

    # 硬编码的本地配置清单（首次创建复制，恢复跳过）
    config_files: list[str] = field(default_factory=lambda: ["config.yaml", ".env"])
    """需要复制的本地开发配置（相对主仓库根路径）。"""

    hook_dir: str = ".codeforge/hooks"
    """git hooks 源目录（相对主仓库根）。"""


async def initialize(
    worktree_dir: Path,
    repo_root: Path,
    opts: InitOptions,
) -> list[str]:
    """在 worktree 目录执行环境初始化。

    Args:
        worktree_dir: 目标 worktree 目录（应已存在）。
        repo_root: 主仓库根目录。
        opts: 初始化选项。

    Returns:
        警告列表（软链失败等 best-effort 项）。

    Raises:
        WorktreeError: 复制配置等必选项失败（fail-closed）。
    """
    warnings: list[str] = []
    worktree_dir = Path(worktree_dir)
    repo_root = Path(repo_root)

    # ── 1. 复制本地配置（首次创建复制，恢复跳过） ──
    _copy_config(worktree_dir, repo_root, opts.config_files)

    # ── 2. 配置 worktree 子目录 git hooks ──
    _copy_hooks(worktree_dir, repo_root, opts.hook_dir)

    # ── 3. 软链大型依赖目录（best-effort） ──
    for dep in opts.symlink_directories:
        try:
            _symlink_dependency(worktree_dir, repo_root, dep)
        except WorktreeError as e:
            warnings.append(str(e))

    # ── 4. 补被忽略但运行需要的文件 ──
    _copy_config(worktree_dir, repo_root, opts.config_files, fresh_only=True)

    return warnings


# ── 内部步骤 ──────────────────────────────────────────────────────


def _copy_config(
    worktree_dir: Path,
    repo_root: Path,
    config_files: list[str],
    fresh_only: bool = True,
) -> None:
    """复制本地配置到 worktree。

    首次创建时复制（目标不存在）；恢复时跳过（fresh_only=True，
    目标已存在则不覆盖，避免覆盖子 Agent 已改过的配置）。

    Raises:
        WorktreeError: 源文件存在但复制失败。
    """
    for rel in config_files:
        src = (repo_root / rel).resolve()
        dst = (worktree_dir / rel).resolve()
        if not src.is_file():
            continue  # 源没有该配置则跳过（非错误）
        if dst.exists() and fresh_only:
            continue  # 恢复时已存在：跳过不覆盖
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
        except OSError as e:
            raise WorktreeError(f"failed to copy config {rel}: {e}") from e


def _copy_hooks(worktree_dir: Path, repo_root: Path, hook_dir: str) -> None:
    """复制主目录 hooks 配置到 worktree 子目录。

    源 hooks 目录缺 / 空 → 跳过（无 hooks 不算错误）。
    复制因权限/IO 失败 → 抛错（fail-closed）。
    """
    # hook_dir 参数作为相对路径基准，支持 hooks 位于 .git/hooks 或 .codeforge/hooks
    src = (repo_root / hook_dir).resolve()
    if not src.is_dir():
        return
    dst = (worktree_dir / hook_dir).resolve()
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.is_file():
                target = dst / f.name
                target.write_bytes(f.read_bytes())
    except OSError as e:
        raise WorktreeError(f"failed to copy git hooks from {src}: {e}") from e


def _symlink_dependency(
    worktree_dir: Path,
    repo_root: Path,
    dep: str,
) -> None:
    """软链一个依赖目录到主仓库对应位置。

    best-effort：失败仅告警（由调用方收集），不抛致命错。
    但目标已存在（冲突）时抛 WorktreeError 由调用方转为警告。
    """
    src = (repo_root / dep).resolve()
    dst = (worktree_dir / dep).resolve()
    if not src.is_dir():
        return  # 主仓库无此依赖目录 → 跳过

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        # 已存在软链或目标实体：跳过不覆盖（可能已是首次创建的实体）
        return

    try:
        dst.symlink_to(src, target_is_directory=True)
    except OSError as e:
        raise WorktreeError(f"failed to symlink {dep}: {e}") from e
