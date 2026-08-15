"""团队协作系统 —— 基础类型。

Team / TeammateInfo / BackendType 数据结构与团队异常。
对齐 `conversation/message.py` 的 `str, Enum` 约定与 `core/task/manager.py` 的自定义异常风格。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class BackendType(str, Enum):
    """队员执行后端类型。"""

    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


@dataclass
class TeammateInfo:
    """一个队员（成员）的完整元数据。对应 config.json 中 members 数组的条目。

    字段下划线命名与 config.json 的 JSON key 一一对应。
    """

    name: str
    agent_id: str
    # 使用的 subagent 定义名；Fork 路径下为 ""。
    agent_type: str = ""
    # 模型覆盖；"" 表 inherit。
    model: str = ""
    # worktree 绝对路径。
    worktree_path: str = ""
    # 对应 worktree 分支名。
    branch: str = ""
    # 可 per-member 不同。
    backend_type: BackendType = BackendType.IN_PROCESS
    # tmux pane id / iterm2 split id；in-process 为空。
    pane_id: str = ""
    # None 或 True 表活跃，False 表空闲；终止后直接从 members 移除。
    is_active: bool | None = None
    plan_mode_required: bool = False
    # 队员独立 session 目录绝对路径。
    session_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "backend_type": self.backend_type.value,
            "pane_id": self.pane_id,
            "is_active": self.is_active,
            "plan_mode_required": self.plan_mode_required,
            "session_dir": self.session_dir,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TeammateInfo:
        return cls(
            name=d.get("name", ""),
            agent_id=d.get("agent_id", ""),
            agent_type=d.get("agent_type", ""),
            model=d.get("model", ""),
            worktree_path=d.get("worktree_path", ""),
            branch=d.get("branch", ""),
            backend_type=BackendType(d.get("backend_type", "in-process")),
            pane_id=d.get("pane_id", ""),
            is_active=d.get("is_active"),
            plan_mode_required=bool(d.get("plan_mode_required", False)),
            session_dir=d.get("session_dir", ""),
        )


@dataclass
class Team:
    """长期存在的小组对象。

    `name` / `sanitized_name` / `lead_agent_id` / `backend` / `members` 持久化到 config.json。
    派生路径（config_dir / config_path / tasks_path / mailbox_dir）按 home_dir 推导，不持久化。
    """

    name: str
    sanitized_name: str
    lead_agent_id: str = "lead"
    backend: BackendType = BackendType.IN_PROCESS
    description: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    members: list[TeammateInfo] = field(default_factory=list)

    # 派生路径（不持久化，由 Manager 构造时填充）。
    config_dir: str = ""
    config_path: str = ""
    tasks_path: str = ""
    mailbox_dir: str = ""

    # 状态锁：所有修改 members 后需持久化的方法，加锁后先 reload disk 再改。
    _lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "sanitized_name": self.sanitized_name,
            "lead_agent_id": self.lead_agent_id,
            "backend": self.backend.value,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "members": [m.to_dict() for m in self.members],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Team:
        team = cls(
            name=d.get("name", ""),
            sanitized_name=d.get("sanitized_name", ""),
            lead_agent_id=d.get("lead_agent_id", "lead"),
            backend=BackendType(d.get("backend", "in-process")),
            description=d.get("description", ""),
            members=[TeammateInfo.from_dict(m) for m in d.get("members", [])],
        )
        raw_created = d.get("created_at")
        if raw_created:
            try:
                team.created_at = datetime.fromisoformat(raw_created)
            except (ValueError, TypeError):
                pass
        return team

    def member_by_name(self, name: str) -> TeammateInfo | None:
        return next((m for m in self.members if m.name == name), None)

    def member_by_agent_id(self, agent_id: str) -> TeammateInfo | None:
        return next((m for m in self.members if m.agent_id == agent_id), None)


class TeamError(Exception):
    """团队异常统一基类。调用方可 except 判别。"""


class TeamNotFoundError(TeamError):
    """指定 Team 不存在。"""


class TeamHasActiveMembersError(TeamError):
    """存在活跃成员，被拒操作（如非 force 删除）。"""


class MemberExistsError(TeamError):
    """成员名在 Team 内已存在。"""


class MemberNotFoundError(TeamError):
    """Team 内找不到该成员。"""


class InProcessTeammateNoSpawnError(TeamError):
    """in-process 后端队员尝试再 spawn 队员（嵌套被禁止）。"""
