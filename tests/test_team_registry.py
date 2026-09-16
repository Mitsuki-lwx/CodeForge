"""AgentNameRegistry 单元测试。

覆盖：register/unregister/resolve/name_of、同名覆盖、同 agent_id 换名、
resolve 接受 agent_id 直接命中。
"""

from __future__ import annotations

from core.team.registry import AgentNameRegistry


def test_register_resolve():
    r = AgentNameRegistry()
    r.register("alice", "agent-123")
    assert r.resolve("alice") == "agent-123"
    assert r.name_of("agent-123") == "alice"


def test_resolve_accepts_agent_id_directly():
    r = AgentNameRegistry()
    r.register("alice", "agent-123")
    assert r.resolve("agent-123") == "agent-123"


def test_unregister_by_name():
    r = AgentNameRegistry()
    r.register("alice", "agent-123")
    r.unregister("alice")
    assert r.resolve("alice") is None
    assert r.name_of("agent-123") is None


def test_unregister_by_agent_id():
    r = AgentNameRegistry()
    r.register("alice", "agent-123")
    r.unregister_by_agent_id("agent-123")
    assert r.resolve("alice") is None
    assert r.resolve("agent-123") is None


def test_same_name_overwrite():
    """同名后注册覆盖前注册（弱引用语义）。"""
    r = AgentNameRegistry()
    r.register("alice", "agent-1")
    r.register("alice", "agent-2")
    assert r.resolve("alice") == "agent-2"
    assert r.name_of("agent-1") is None  # 旧 agent_id 反查被清


def test_same_agent_id_reassigned_name():
    """同 agent_id 换成新 name，旧 name 映射被清。"""
    r = AgentNameRegistry()
    r.register("alice", "agent-1")
    r.register("bob", "agent-1")  # alice 的 agent-1 被 bob 接管
    assert r.name_of("agent-1") == "bob"
    assert r.resolve("alice") is None
    assert r.resolve("bob") == "agent-1"


def test_list():
    r = AgentNameRegistry()
    r.register("alice", "agent-1")
    r.register("bob", "agent-2")
    assert r.list_() == {"alice": "agent-1", "bob": "agent-2"}
