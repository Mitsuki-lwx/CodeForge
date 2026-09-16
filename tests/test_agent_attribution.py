"""多智能体 span 归属：codeforge.agent.* 身份上下文 + span 属性。"""

from __future__ import annotations

import os
from pathlib import Path

from core.observability.context import (
    get_agent_identity,
    reset_agent_identity,
    set_agent_identity,
)

# ── context 函数往返 ───────────────────────────────────────────────


def test_agent_identity_default_none():
    reset_agent_identity()
    assert get_agent_identity() is None


def test_agent_identity_set_get_roundtrip():
    reset_agent_identity()
    set_agent_identity("sess-sub", "vowels-agent")
    assert get_agent_identity() == ("sess-sub", "vowels-agent")
    reset_agent_identity()
    assert get_agent_identity() is None


def test_agent_identity_none_none_clears():
    reset_agent_identity()
    set_agent_identity("x", "y")
    set_agent_identity(None, None)
    assert get_agent_identity() is None


# ── span 归属：span 发射辅助函数打 codeforge.agent.* ───────────────


class _FakeSpan:
    def __init__(self) -> None:
        self.attrs: dict = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v


def test_set_agent_attrs_stamps_on_llm_span():
    from llm import llm_span

    reset_agent_identity()
    set_agent_identity("parent-sub", "vowels-agent")
    span = _FakeSpan()
    llm_span._set_agent_attrs(span)
    assert span.attrs.get("codeforge.agent.id") == "parent-sub"
    assert span.attrs.get("codeforge.agent.name") == "vowels-agent"
    reset_agent_identity()


def test_stamp_agent_attrs_stamps_on_tool_span():
    from conversation.manager import ConversationManager
    from core.agent.agent import Agent, _stamp_agent_attrs
    from core.agent.config import AgentConfig
    from core.tool.context import ExecutionContext

    class _Cfg:
        model = "t"

    class _Client:
        config = _Cfg()

    a = Agent(
        registry=None,
        llm_client=_Client(),
        exec_ctx=ExecutionContext(cwd=Path(os.getcwd()), session_id="parent"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
    )
    a.set_agent_name("fmt-agent")
    reset_agent_identity()

    span = _FakeSpan()
    _stamp_agent_attrs(span, a)
    # 无 ContextVar 时兜底用 agent 自身：id=session_id、name=fmt-agent
    assert span.attrs.get("codeforge.agent.id") == "parent"
    assert span.attrs.get("codeforge.agent.name") == "fmt-agent"
