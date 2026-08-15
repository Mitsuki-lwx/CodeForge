"""WorktreeManager —— worktree 完整生命周期。

创建（含快速恢复）、进入、退出、删除、清理、恢复。
目录放在 <repo>/.codeforge/worktrees/<name>，通过 .git/info/exclude 排除。
explicit cwd 机制保证子 Agent 工具调用落在 worktree，进程级 cwd 不变。
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.worktree.filter import can_auto_clean
from core.worktree.init import InitOptions, WorktreeError, initialize
from core.worktree.safe_name import is_safe_name
from core.worktree.session_store import SessionStore, WorktreeSession

logger = logging.getLogger(__name__)


class WorktreeNameError(WorktreeError):
    """worktree 目录名非法。"""


@dataclass
class CleanupResult:
    """清理结果。"""

    kept: bool                 # True = 保留（有变更），False = 已清理
    reason: str = ""           # 保留原因


class WorktreeManager:
    """管理仓库内的 worktree 工作目录。"""

    def __init__(
        self,
        repo_root: str | Path,
        symlink_directories: list[str] | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._worktrees_dir = self._repo_root / ".codeforge" / "worktrees"
        self._store = SessionStore(self._repo_root)
        self._symlink_dirs = list(symlink_directories or [])

    @property
    def store(self) -> SessionStore:
        """会话注册表。"""
        return self._store

    # ── 创建 ───────────────────────────────────────────────────────

    async def create(
        self,
        name: str,
        branch: str | None = None,
        owner: str = "agent",
    ) -> WorktreeSession:
        """创建（或快速恢复）一个 worktree 工作目录。

        Args:
            name: 目录名（严格校验）。
            branch: 可选分支名；None 时从当前仓库分支派生。
            owner: agent / wf / user。

        Returns:
            创建的会话记录。

        Raises:
            WorktreeNameError: 目录名非法。
            WorktreeError: git worktree add 或初始化失败。
        """
        if not is_safe_name(name):
            raise WorktreeNameError(f"invalid worktree name: {name!r}")

        wt_dir = self._worktrees_dir / name

        # 分支名从目录名派生（安全化：替换非法字符）
        if branch:
            wb = branch
        else:
            wb = f"worktree/{_safe_branch(name)}"

        try:
            wt_dir.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise WorktreeError(f"failed to create worktrees dir: {e}") from e

        if wt_dir.is_dir():
            # 快速恢复：目录已存在，只读文件系统，不调 git
            logger.info("worktree %s exists — quick recovery", name)
        else:
            if not self._git_available():
                raise WorktreeError("git is not available in this repository")
            self._git_worktree_add(wb, wt_dir, name)

        # 环境初始化
        opts = InitOptions(symlink_directories=self._symlink_dirs)
        warnings = await initialize(wt_dir, self._repo_root, opts)
        for w in warnings:
            logger.warning("worktree %s init warning: %s", name, w)

        session = WorktreeSession(
            name=name,
            path=str(wt_dir.resolve()),
            branch=wb,
            workDir=str(self._repo_root.resolve()),
            owner=owner,
            createdAt=time.time(),
        )
        self._store.register(session)

        # 从 exclude 排除（确保不被 git 追踪）
        self._ensure_excluded()
        return session

    # ── 进入 / 退出 ────────────────────────────────────────────────

    async def enter(self, name: str) -> WorktreeSession:
        """进入一个 worktree 会话（读取记录，返回路径供设置 cwd）。

        Args:
            name: worktree 名。

        Returns:
            会话记录。

        Raises:
            WorktreeError: 会话不存在或无 path。
        """
        session = self._store.get(name)
        if session is None:
            raise WorktreeError(f"no worktree session named '{name}'")
        if not session.path or not Path(session.path).is_dir():
            raise WorktreeError(
                f"worktree '{name}' directory missing: {session.path}"
            )
        return session

    async def exit(self, name: str) -> None:
        """退出一个 worktree 会话（清注册表条目）。"""
        self._store.unregister(name)

    # ── 删除 / 清理 ────────────────────────────────────────────────

    async def delete(self, name: str) -> tuple[bool, str]:
        """删除一个 worktree（含三层过滤变更保护）。

        Args:
            name: worktree 名。

        Returns:
            (成功与否, 原因)。成功时原因 ""；拒绝时原因说明。
        """
        session = self._store.get(name)
        if session is None:
            return False, f"no worktree session named '{name}'"

        result = self._cleanup(session)
        if result.kept:
            return False, result.reason
        return True, ""

    async def cleanup(self, session: WorktreeSession) -> CleanupResult:
        """清理一个 worktree（子 Agent 返回时调用）。

        三层过滤；有未提交修改/未推送 commit → 保留 + 报错提示主 Agent。
        """
        return self._cleanup(session)

    # ── 列表 / 恢复 ────────────────────────────────────────────────

    def list_active(self) -> list[WorktreeSession]:
        """列出全部活跃会话。"""
        return self._store.active()

    def recover_all(self) -> list[WorktreeSession]:
        """启动时恢复全部未退出的 worktree 会话。

        Returns:
            需恢复的会话列表（供 Agent 重建 cwd）。
        """
        sessions = self._store.active()
        if sessions:
            logger.info("recovering %d unfinished worktree session(s)", len(sessions))
        return sessions

    async def cleanup_all_generated(self) -> None:
        """清理全部可自动清理的过期 worktree（退出前调用）。

        只清 agent-/wf_ 前缀且干净的工作树；用户创建的永不清。
        """
        for session in self._store.active():
            try:
                self._cleanup(session)
            except Exception as e:  # noqa: BLE001 —— 清理失败仅告警
                logger.warning("cleanup_all worktree %s failed: %s", session.name, e)

    # ── 内部 ───────────────────────────────────────────────────────

    def _cleanup(self, session: WorktreeSession) -> CleanupResult:
        """按三层过滤清理单个 worktree。"""
        check = can_auto_clean(
            self._repo_root, session.name, session.path
        )
        if not check.ok:
            return CleanupResult(kept=True, reason=check.reason)

        wt_dir = Path(session.path)
        try:
            self._git_worktree_remove(wt_dir, session.name)
            self._git_branch_delete(session.branch)
        except WorktreeError as e:
            return CleanupResult(kept=True, reason=str(e))

        # 兜底删残留目录
        if wt_dir.exists() and wt_dir.is_dir():
            import shutil

            try:
                shutil.rmtree(wt_dir)
            except OSError as e:
                return CleanupResult(kept=True, reason=f"failed to remove dir: {e}")

        self._store.unregister(session.name)
        return CleanupResult(kept=False)

    # ── git 辅助 ───────────────────────────────────────────────────

    def _git_available(self) -> bool:
        try:
            subprocess.run(
                ["git", "-C", str(self._repo_root), "rev-parse", "--git-dir"],
                capture_output=True, timeout=5,
            )
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _git_worktree_add(self, branch: str, wt_dir: Path, name: str) -> None:
        """git worktree add -b <branch> <dir>。"""
        try:
            r = subprocess.run(
                [
                    "git", "-C", str(self._repo_root),
                    "worktree", "add", "-b", branch, str(wt_dir),
                ],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WorktreeError(f"git worktree add failed: {e}") from e
        if r.returncode != 0:
            raise WorktreeError(
                f"git worktree add failed for '{name}': {r.stderr.strip()}"
            )

    def _git_worktree_remove(self, wt_dir: Path, name: str) -> None:
        """git worktree remove <dir> --force。"""
        try:
            r = subprocess.run(
                ["git", "-C", str(self._repo_root), "worktree", "remove",
                 "--force", str(wt_dir)],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WorktreeError(f"git worktree remove failed: {e}") from e
        if r.returncode != 0:
            raise WorktreeError(
                f"git worktree remove failed for '{name}': {r.stderr.strip()}"
            )

    def _git_branch_delete(self, branch: str) -> None:
        """删除 worktree 分支。"""
        if not branch:
            return
        try:
            r = subprocess.run(
                ["git", "-C", str(self._repo_root), "branch", "-D", branch],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise WorktreeError(f"git branch -D failed: {e}") from e
        if r.returncode != 0:
            # 分支删除失败不致命（可能已删），仅告警
            logger.warning("git branch -D %s failed: %s", branch, r.stderr.strip())

    def _ensure_excluded(self) -> None:
        """确保 .codeforge/worktrees 被 git exclude（不被追踪）。"""
        try:
            git_dir = subprocess.run(
                ["git", "-C", str(self._repo_root), "rev-parse", "--git-path", "info"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not git_dir:
                git_dir = str(self._repo_root / ".git" / "info")
            exclude = Path(git_dir) / "exclude"
            marker = ".codeforge/worktrees/"
            if exclude.exists() and marker in exclude.read_text(encoding="utf-8"):
                return
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(f"\n{marker}\n")
        except OSError as e:
            logger.warning("failed to ensure worktrees exclusion: %s", e)


def _safe_branch(name: str) -> str:
    """将目录名安全化为合法 git 分支名。"""
    import re

    s = re.sub(r"[^a-zA-Z0-9._-]", "-", name)
    return s.replace("/", "-")
