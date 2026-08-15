"""Worktree 会话注册表 —— worktree_session.json。

仓库根目录下的共享注册文件，记录所有活跃的 worktree 会话。
崩溃/重启后据此恢复未退出的会话；正常退出（exitWorktree）清空对应条目。
支持多会话。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class WorktreeSession:
    """一个活跃 worktree 会话的记录。"""

    name: str                          # 目录名（也用于匹配是否自动清理）
    path: str                          # worktree 绝对路径
    branch: str = ""                   # worktree 分支（展示/辅助决策用）
    workDir: str = ""                  # 主仓库根
    owner: str = "agent"               # agent / wf / user
    createdAt: float = 0.0             # epoch seconds


class SessionStore:
    """worktree_session.json 注册表。

    以仓库根为基准，文件放在 <repo>/.codeforge/worktree_session.json。
    文件损坏时不抛错（告警 + 返回空），不阻塞启动。
    """

    FILENAME = "worktree_session.json"
    DIRNAME = ".codeforge"

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = Path(repo_root)
        self._store_dir = self._repo_root / self.DIRNAME
        self._path = self._store_dir / self.FILENAME

    @property
    def path(self) -> Path:
        """注册表文件路径。"""
        return self._path

    # ── 读写 ───────────────────────────────────────────────────────

    def load(self) -> list[WorktreeSession]:
        """读取全部活跃会话。

        Returns:
            会话列表。文件不存在返回空；损坏告警 + 返回空。
        """
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("worktree_session.json unreadable: %s", e)
            return []

        sessions = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    sessions.append(_session_from_dict(item))
        return sessions

    def load_as_map(self) -> dict[str, WorktreeSession]:
        """读取并转为 name → session 映射。"""
        return {s.name: s for s in self.load()}

    # ── 修改 ───────────────────────────────────────────────────────

    def register(self, session: WorktreeSession) -> None:
        """注册（或覆盖同名）一个会话。"""
        current = self.load_as_map()
        current[session.name] = session
        self._write(list(current.values()))

    def unregister(self, name: str) -> None:
        """注销一个会话（按名字）。"""
        current = self.load_as_map()
        if name in current:
            del current[name]
            self._write(list(current.values()))

    def get(self, name: str) -> WorktreeSession | None:
        """按名字查询会话。"""
        return self.load_as_map().get(name)

    def active(self) -> list[WorktreeSession]:
        """当前全部活跃会话（同 load）。"""
        return self.load()

    def clear_all(self) -> None:
        """清空全部会话（退出兜底用）。"""
        self._write([])

    def _write(self, sessions: list[WorktreeSession]) -> None:
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            # ensure_ascii=False 保留中文路径；sort_keys 稳定输出
            payload = json.dumps(
                [asdict(s) for s in sessions],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            self._path.write_text(payload, encoding="utf-8")
        except OSError as e:
            logger.error("failed to write worktree_session.json: %s", e)


def _session_from_dict(d: dict) -> WorktreeSession:
    """从 dict 构造 WorktreeSession（容错缺字段）。"""
    return WorktreeSession(
        name=str(d.get("name", "")),
        path=str(d.get("path", "")),
        branch=str(d.get("branch", "")),
        workDir=str(d.get("workDir", "")),
        owner=str(d.get("owner", "agent")),
        createdAt=float(d.get("createdAt", 0.0)),
    )
