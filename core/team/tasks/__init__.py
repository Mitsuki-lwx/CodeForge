"""共享任务列表 —— Task / Status / Store / Filter / Patch。

每个 Team 一个 `tasks.json`（<team_config_dir>/tasks.json），read-modify-write + 文件锁，
跨进程/in-process 多成员并发安全。
`add_blocked_by` / `add_blocks` 会双向维护依赖关系（A.blocked_by=[B] ⟺ B.blocks=[A]）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from core.team import filelock

# tasks.json 的容器键。
_TASKS_KEY = "tasks"


class Status(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    status: Status = Status.PENDING
    assignee: str = ""
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0
    # 派生字段：list_ 时计算，不落盘。
    is_ready: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "assignee": self.assignee,
            "blocked_by": list(self.blocked_by),
            "blocks": list(self.blocks),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            status=Status(d.get("status", "pending")),
            assignee=d.get("assignee", ""),
            blocked_by=list(d.get("blocked_by", [])),
            blocks=list(d.get("blocks", [])),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
        )


@dataclass
class Filter:
    """list_ 过滤条件。"""

    status: Status | None = None


@dataclass
class Patch:
    """update 的可选变更字段。"""

    title: str | None = None
    description: str | None = None
    status: Status | None = None
    assignee: str | None = None
    add_blocks: list[str] | None = None
    add_blocked_by: list[str] | None = None
    remove_blocks: list[str] | None = None
    remove_blocked_by: list[str] | None = None


class Store:
    """Team 的共享任务存储。"""

    def __init__(self, path: str) -> None:
        self._path = str(path)

    def _lock(self) -> Path:
        # tasks.json → tasks.lock
        return Path(self._path).with_suffix(".lock")

    # ── 内部读写 ──────────────────────────────────────────────────

    def _read_tasks(self) -> list[dict]:
        p = Path(self._path)
        if not p.exists():
            return []
        try:
            import json

            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return list(data.get(_TASKS_KEY, []))
        except (OSError, ValueError):
            return []
        return []

    def _write_tasks(self, tasks: list[dict]) -> None:
        from core.team.persistence import atomic_write_json

        atomic_write_json(self._path, {_TASKS_KEY: tasks})

    # ── 公开 CRUD ─────────────────────────────────────────────────

    async def create(self, t: Task) -> str:
        """新建任务，返回任务 id（task_<6 位 hex>）。"""
        t.id = f"task_{secrets.token_hex(3)}"
        now = int(time.time())
        t.created_at = now
        t.updated_at = now
        async with filelock.acquire(self._lock()):
            tasks = self._read_tasks()
            tasks.append(t.to_dict())
            self._write_tasks(tasks)
        return t.id

    async def get(self, id_: str) -> Task | None:
        async with filelock.acquire(self._lock()):
            for d in self._read_tasks():
                if d.get("id") == id_:
                    return Task.from_dict(d)
        return None

    async def list_(self, f: Filter | None = None) -> list[Task]:
        async with filelock.acquire(self._lock()):
            raw = self._read_tasks()
        by_id = {d["id"]: Task.from_dict(d) for d in raw if d.get("id")}
        tasks = list(by_id.values())
        for t in tasks:
            t.is_ready = all(
                by_id[b].status is Status.COMPLETED
                for b in t.blocked_by
                if b in by_id
            ) and all(b in by_id for b in t.blocked_by)
        if f is not None and f.status is not None:
            tasks = [t for t in tasks if t.status is f.status]
        tasks.sort(key=lambda t: t.created_at)
        return tasks

    async def update(self, id_: str, p: Patch) -> Task | None:
        async with filelock.acquire(self._lock()):
            tasks = self._read_tasks()
            idx = next(
                (i for i, d in enumerate(tasks) if d.get("id") == id_), None
            )
            if idx is None:
                return None
            t = Task.from_dict(tasks[idx])

            if p.title is not None:
                t.title = p.title
            if p.description is not None:
                t.description = p.description
            if p.status is not None:
                t.status = p.status
            if p.assignee is not None:
                t.assignee = p.assignee
            if p.add_blocks:
                t.blocks = _uniq(t.blocks + p.add_blocks)
            if p.remove_blocks:
                t.blocks = [b for b in t.blocks if b not in p.remove_blocks]
            if p.add_blocked_by:
                t.blocked_by = _uniq(t.blocked_by + p.add_blocked_by)
            if p.remove_blocked_by:
                t.blocked_by = [b for b in t.blocked_by if b not in p.remove_blocked_by]

            # 提交自身变更
            t.updated_at = int(time.time())
            tasks[idx] = t.to_dict()

            # ── 双向维护依赖 ──
            # src.add_blocks=[x] ⟹ src.blocks ∋ x ⟹ x.blocked_by ∋ src
            if p.add_blocks:
                _sync_deps(tasks, src_id=id_, targets=p.add_blocks, op="add",
                           reverse_field="blocked_by")
            if p.remove_blocks:
                _sync_deps(tasks, src_id=id_, targets=p.remove_blocks, op="remove",
                           reverse_field="blocked_by")
            # src.add_blocked_by=[x] ⟹ src.blocked_by ∋ x ⟹ x.blocks ∋ src
            if p.add_blocked_by:
                _sync_deps(tasks, src_id=id_, targets=p.add_blocked_by, op="add",
                           reverse_field="blocks")
            if p.remove_blocked_by:
                _sync_deps(tasks, src_id=id_, targets=p.remove_blocked_by, op="remove",
                           reverse_field="blocks")

            self._write_tasks(tasks)
            return t


def _sync_deps(
    tasks: list[dict],
    src_id: str,
    targets: list[str],
    op: str,
    reverse_field: str,
) -> None:
    """在 target 任务的 `reverse_field`（blocked_by 或 blocks）上反向追加/移除 src_id。"""
    target_set = set(targets)
    for d in tasks:
        did = d.get("id")
        if did not in target_set:
            continue
        lst = list(d.get(reverse_field, []))
        if op == "add":
            lst = _uniq(lst + [src_id])
        else:  # remove
            lst = [x for x in lst if x != src_id]
        d[reverse_field] = lst


def _uniq(seq: list[str]) -> list[str]:
    """去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


__all__ = ["Filter", "Patch", "Status", "Store", "Task"]
