"""Agent 名称注册表 —— name ↔ agent_id 双向映射。

`task.Manager` 原有的 `_by_name` 局部 dict 升级为统一注册表（F35–F38）。
后注册的覆盖前注册（弱引用语义）。用 threading.Lock 保护，跨协程线程安全。
"""

from __future__ import annotations

import threading


class AgentNameRegistry:
    """队员名 / agent_id 的弱引用双向映射。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_name: dict[str, str] = {}  # name → agent_id
        self._by_id: dict[str, str] = {}  # agent_id → name

    def register(self, name: str, agent_id: str) -> None:
        """注册 name → agent_id。同名覆盖旧映射；同 agent_id 换名也反向修正。"""
        with self._lock:
            # 同 agent_id 已被别的 name 占用：清掉旧 name 映射
            old_name = self._by_id.get(agent_id)
            if old_name is not None and old_name != name:
                self._by_name.pop(old_name, None)
            # 旧 name 已被占用：覆盖并清理被覆盖 agent_id 的反查
            old_id = self._by_name.get(name)
            if old_id is not None and old_id != agent_id:
                self._by_id.pop(old_id, None)
            self._by_name[name] = agent_id
            self._by_id[agent_id] = name

    def unregister(self, name: str) -> None:
        """按 name 注销（连带清理 agent_id 反查）。"""
        with self._lock:
            agent_id = self._by_name.pop(name, None)
            if agent_id is not None:
                self._by_id.pop(agent_id, None)

    def unregister_by_agent_id(self, agent_id: str) -> None:
        """按 agent_id 注销。"""
        with self._lock:
            name = self._by_id.pop(agent_id, None)
            if name is not None:
                self._by_name.pop(name, None)

    def resolve(self, name_or_id: str) -> str | None:
        """把 name 或 agent_id 统一解析为 agent_id。name 优先，失败按 agent_id 反查。"""
        with self._lock:
            if name_or_id in self._by_name:
                return self._by_name[name_or_id]
            # 传入的本身可能就是 agent_id
            if name_or_id in self._by_id:
                return name_or_id
            return None

    def name_of(self, agent_id: str) -> str | None:
        with self._lock:
            return self._by_id.get(agent_id)

    def list_(self) -> dict[str, str]:
        with self._lock:
            return dict(self._by_name)


__all__ = ["AgentNameRegistry"]
