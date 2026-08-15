"""邮箱 —— Box：队员间的点对点消息读写。

每个收件人独占一个 lock 文件（<dir>/<agent_id>.lock），用 core.team.filelock.acquire
保证跨进程/in-process 多 task 的 read-modify-write 串行。消息文件为
<dir>/<agent_id>.json，结构 {"messages":[...]}。
"""

from __future__ import annotations

import time
from pathlib import Path

from core.team import filelock
from core.team.mailbox.message import Message

# 单个邮箱文件的 JSON 容器键。
_MESSAGES_KEY = "messages"


def _default_box() -> dict:
    return {_MESSAGES_KEY: []}


class Box:
    """一个团队的邮箱目录（<team_config_dir>/mailbox/）。"""

    def __init__(self, dir_: str) -> None:
        self._dir = str(dir_)

    def _path(self, agent_id: str) -> Path:
        return Path(self._dir) / f"{agent_id}.json"

    def _lock(self, agent_id: str) -> Path:
        return Path(self._dir) / f"{agent_id}.lock"

    def _read_raw(self, agent_id: str) -> dict:
        p = self._path(agent_id)
        if not p.exists():
            return _default_box()
        try:
            import json

            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return _default_box()

    def _write_raw(self, agent_id: str, data: dict) -> None:
        from core.team.persistence import atomic_write_json

        atomic_write_json(self._path(agent_id), data)

    # ── 公开 API ──────────────────────────────────────────────────

    async def write(self, agent_id: str, msg: Message) -> None:
        """向 agent_id 的邮箱追加一条消息（read-modify-write + 锁）。"""
        msg.to = agent_id
        if msg.timestamp == 0:
            msg.timestamp = int(time.time())
        async with filelock.acquire(self._lock(agent_id)):
            data = self._read_raw(agent_id)
            msgs = list(data.get(_MESSAGES_KEY, []))
            msgs.append(msg.to_dict())
            self._write_raw(agent_id, {_MESSAGES_KEY: msgs})

    async def read(self, agent_id: str) -> list[Message]:
        """返回该收件人的全部消息（含已读）。"""
        async with filelock.acquire(self._lock(agent_id)):
            data = self._read_raw(agent_id)
        return [Message.from_dict(m) for m in data.get(_MESSAGES_KEY, [])]

    async def read_unread(
        self, agent_id: str
    ) -> tuple[list[int], list[Message]]:
        """读回未读消息：返回 (indices, messages)。读取本身不改动 read 状态。"""
        msgs = await self.read(agent_id)
        unread_indices: list[int] = []
        unread: list[Message] = []
        for i, m in enumerate(msgs):
            if not m.read:
                unread_indices.append(i)
                unread.append(m)
        return unread_indices, unread

    async def mark_read(self, agent_id: str, indices: list[int]) -> None:
        """按索引把对应消息标记为 read。"""
        if not indices:
            return
        index_set = set(indices)
        async with filelock.acquire(self._lock(agent_id)):
            data = self._read_raw(agent_id)
            msgs = data.get(_MESSAGES_KEY, [])
            changed = False
            for i, m in enumerate(msgs):
                if i in index_set and not m.get("read"):
                    m["read"] = True
                    changed = True
            if changed:
                self._write_raw(agent_id, {_MESSAGES_KEY: msgs})


__all__ = ["Box", "Message"]
