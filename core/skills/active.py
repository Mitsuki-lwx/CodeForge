"""跨轮激活 Skill 列表管理。

激活的 Skill 在每轮 environment 重建时注入 SOP。
支持重复激活（覆盖 body），/clear 时全部清空。
"""

from __future__ import annotations

import threading

from core.skills.types import ActiveEntry


class ActiveSkills:
    """跨轮激活 Skill 列表，线程安全。

    保持激活顺序；同名重复激活时覆盖原 body，不改变位置。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[ActiveEntry] = []
        self._index: dict[str, int] = {}  # name → 下标

    def activate(self, name: str, body: str) -> None:
        """激活一个 Skill。

        如果已存在同名 Skill，原地更新 body（不改变位置）。
        否则追加到列表末尾。

        Args:
            name: Skill 名称。
            body: 激活时的 SOP 正文。
        """
        entry = ActiveEntry(name=name, body=body)
        with self._lock:
            if name in self._index:
                idx = self._index[name]
                self._entries[idx] = entry
            else:
                self._index[name] = len(self._entries)
                self._entries.append(entry)

    def clear(self) -> None:
        """清空全部已激活 Skill。"""
        with self._lock:
            self._entries.clear()
            self._index.clear()

    def snapshot(self) -> list[ActiveEntry]:
        """返回当前激活 Skill 的快照拷贝。"""
        with self._lock:
            return list(self._entries)

    def names(self) -> list[str]:
        """返回当前激活 Skill 的名字列表（保持激活顺序）。"""
        with self._lock:
            return [e.name for e in self._entries]
