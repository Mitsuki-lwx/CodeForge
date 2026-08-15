"""Worktree 清理安全过滤 —— 三层检查。

判断一个 worktree 目录是否可安全删除：
  1. 名字匹配生成模式（用户创建的不自动清理）
  2. 目录确实在 <repo>/.codeforge/worktrees/ 下（防路径逃逸）
  3. 无未提交修改 + 无未推送 commit

三层全过才能删。绝不删除主工作目录本身。
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.worktree.safe_name import is_generated_name

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """过滤判定结果。"""

    ok: bool
    reason: str = ""


def can_auto_clean(repo_root: Path, name: str, worktree_path: str) -> FilterResult:
    """三层安全检查：判断一个 worktree 是否可自动清理。

    Args:
        repo_root: 主仓库根目录。
        name: worktree 名字。
        worktree_path: worktree 目录绝对路径。

    Returns:
        FilterResult；ok=True 表示可清理。
    """
    # ── 第一层：名字匹配生成模式 ──
    if not is_generated_name(name):
        return FilterResult(False, f"'{name}' is user-created, never auto-cleaned")

    # ── 第二层：目录确实在 worktrees 目录下（防逃逸） ──
    wt_base = (Path(repo_root) / ".codeforge" / "worktrees").resolve()

    # 主工作目录本身绝不清理
    if Path(worktree_path).resolve() == Path(repo_root).resolve():
        return FilterResult(False, "refusing to clean the main working directory")

    try:
        resolved = Path(worktree_path).resolve()
        resolved.relative_to(wt_base)
    except ValueError:
        return FilterResult(
            False,
            f"worktree path '{worktree_path}' is outside {wt_base}",
        )

    # 目录必须真实存在
    if not resolved.is_dir():
        return FilterResult(False, f"worktree directory '{resolved}' does not exist")

    # ── 第三层：无未提交修改 + 无未推送 commit ──
    # 用 git 在 worktree 本身判断（worktree 上有自己的 HEAD）
    clean, reason = _check_dirty(resolved)
    if not clean:
        return FilterResult(False, reason)

    return FilterResult(True, "")


def _check_dirty(worktree_dir: Path) -> tuple[bool, str]:
    """检查 worktree 是否脏（有未提交修改或未推送 commit）。

    Returns:
        (is_clean, reason_if_dirty)
    """
    # 未提交修改
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("git status failed in %s: %s", worktree_dir, e)
        return True, ""  # git 不可用时不阻塞清理（fail-open），但上层目录已验证存在
    if r.returncode != 0:
        logger.warning("git status returned %d in %s", r.returncode, worktree_dir)
        return True, ""
    if r.stdout.strip():
        return False, "has uncommitted changes"

    # 未推送 commit
    try:
        r2 = subprocess.run(
            ["git", "-C", str(worktree_dir), "log", "@{u}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("git log failed in %s: %s", worktree_dir, e)
        return True, ""
    if r2.returncode != 0:
        # 无上游分支视为无未推送 commit（新分支）
        return True, ""
    if r2.stdout.strip():
        return False, "has unpushed commits"

    return True, ""
