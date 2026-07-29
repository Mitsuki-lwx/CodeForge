"""环境信息采集与格式化。

收集运行环境信息（工作目录、OS、日期、git 状态、版本、模型），
构造成一段人类可读的文本供模型感知上下文。
"""

from __future__ import annotations

import datetime
import os
import platform
import subprocess
from pathlib import Path


def collect_environment(
    work_dir: str | Path = ".",
    model: str = "",
    version: str = "0.1.0",
) -> str:
    """采集当前运行环境信息，返回一段人类可读的短文本。

    Args:
        work_dir: 当前工作目录
        model: 当前使用的模型名
        version: 应用版本

    Returns:
        格式化的环境信息文本（≤500 字符）。各项采集失败时降级留空。
    """
    cwd = str(Path(work_dir).resolve())
    os_name = platform.system()
    date_str = datetime.date.today().isoformat()
    git_branch, git_clean = _collect_git_status(cwd)

    lines = [
        "## Environment",
        f"Working directory: {cwd}",
        f"Platform: {os_name}",
        f"Date: {date_str}",
    ]

    if git_branch:
        status = "clean" if git_clean else "dirty"
        lines.append(f"Git branch: {git_branch} ({status})")

    if model:
        lines.append(f"Model: {model}")
    if version:
        lines.append(f"CodeForge version: {version}")

    return "\n".join(lines)


def _collect_git_status(cwd: str) -> tuple[str, bool]:
    """采集 git 状态：返回 (branch_name, is_clean)。

    采集失败时返回 ("", False)，不抛异常、不阻塞。
    """
    try:
        # 尝试 GitPython
        from git import InvalidGitRepositoryError, Repo

        try:
            repo = Repo(cwd, search_parent_directories=True)
            # 获取当前分支名
            branch = ""
            try:
                branch = repo.active_branch.name
            except (TypeError, ValueError):
                # 处于 detached HEAD 状态
                try:
                    branch = repo.head.commit.hexsha[:7]
                except Exception:
                    branch = "HEAD"

            # 检查是否 clean
            is_clean = not repo.is_dirty(untracked_files=True)
            return branch, is_clean

        except InvalidGitRepositoryError:
            pass

    except ImportError:
        pass

    # 回退：subprocess 跑 git 命令
    try:
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=3,
        )
        branch = branch_result.stdout.strip()
        if not branch:
            # detached HEAD → 取 commit hash
            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=3,
            )
            branch = hash_result.stdout.strip() or "HEAD"

        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=3,
        )
        is_clean = status_result.stdout.strip() == ""
        return branch, is_clean

    except Exception:
        return "", False
