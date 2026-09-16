"""Agent 工具 team_name 分支单测。

验证：不带 team_name 走原路径；带 team_name 委托给 team_hook.spawn_teammate；
in-process 队员上下文时拒绝（InProcessTeammateNoSpawnError）。
"""

from __future__ import annotations

from core.agent.team_hook import TeammateContext
from core.tool.registry import ToolRegistry
from core.tool.tools.agent_tool import AgentTool


class _FakeParent:
    """极简父 Agent：仅提供 Agent 工具路径需要的最小表面。"""

    def __init__(self) -> None:
        self._conversation = None
        self._registry = ToolRegistry()
        self._client = None
        self._runtime = None
        self._hooks = None
        self._exec_ctx = None


class _FakeHook:
    """假 TeamHook：记录 spxt 调用并返回固定文本。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def spawn_teammate(self, **kw):
        self.calls.append(kw)
        return "队员 alice 已派生"


def _make_tool(hook=None):
    return AgentTool(
        catalog=None,
        task_mgr=None,
        parent_agent=_FakeParent(),
        wt_manager=None,
        team_hook=hook,
    )


async def test_agent_no_team_name_still_requires_prompt():
    tool = _make_tool(hook=_FakeHook())
    res = await tool.execute(None, {"prompt": "", "description": "x"})
    assert res.success is False
    assert "prompt" in res.error


async def test_agent_team_name_delegates_to_hook():
    hook = _FakeHook()
    tool = _make_tool(hook=hook)
    res = await tool.execute(
        None,
        {
            "team_name": "demo",
            "name": "alice",
            "prompt": "do work",
            "description": "x",
            "subagent_type": "general-purpose",
        },
    )
    assert res.success is True
    assert "alice" in res.data
    assert hook.calls, "team_hook.spawn_teammate 应被调用"
    assert hook.calls[0]["team_name"] == "demo"
    assert hook.calls[0]["member_name"] == "alice"


async def test_agent_team_name_without_hook_errors():
    tool = _make_tool(hook=None)
    res = await tool.execute(
        None, {"team_name": "demo", "prompt": "x", "description": "x"}
    )
    assert res.success is False
    assert "TeamHook" in res.error or "团队系统未启用" in res.error


async def test_inprocess_teammate_cannot_spawn(monkeypatch):
    """in-process 队员 ctx 激活时，team_name spawn 被拒。"""
    monkeypatch.delenv("CODEFORGE_TEAM_HOOK", raising=False)

    tc = TeammateContext(
        team_name="demo", member_name="alice", agent_id="agent-a",
        backend_type="in-process",
    )
    tc.install()
    try:
        hook = _FakeHook()
        tool = _make_tool(hook=hook)
        res = await tool.execute(
            None, {"team_name": "demo", "prompt": "x", "description": "x"}
        )
        assert res.success is False
        assert "InProcessTeammateNoSpawnError" in res.error or "in-process" in res.error
        assert not hook.calls
    finally:
        tc.uninstall()


def test_input_schema_has_team_name():
    tool = _make_tool()
    schema = tool.input_schema()
    assert "team_name" in schema["properties"]
