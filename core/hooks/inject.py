"""会话级注入文本存储。"""

from __future__ import annotations


class InjectionStore:
    """保存 prompt 动作注入的文本，供每轮 system prompt 拼装。"""

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []

    def add(self, rule_name: str, content: str) -> None:
        self._items.append((rule_name, content))

    def snapshot(self) -> list[str]:
        return [content for _, content in self._items]

    def clear(self) -> None:
        self._items.clear()
