"""子 Agent 进度可见性：进度汇 + 看门狗文案。

**背景（实测确认的结构性事实，不是取舍）**：父 Agent 执行工具（含子 Agent）时自己是
`await` 状态，它的事件流生成器正挂在这个 await 上、**不能 yield**，所以子 Agent 的
进度不可能经由父的事件流实时冒出来。而此前界面在该阶段只显示「仍在等待模型响应」
——**那句是误导**：等的不是模型，是子 Agent（实测派了子 Agent 之后一直这么显示）。

现在的形态：子 Agent 把「谁在做什么」写进一个汇点（`core/tool/context.ProgressSink`），
界面在静默看门狗醒来时**主动读**它。看门狗本身由 `test_tui_stall.py` 覆盖，
这里覆盖汇点行为、文案、以及"子 Agent 必须继承父的汇"这条接缝。
"""

from __future__ import annotations

from pathlib import Path

from core.tool.context import ExecutionContext, ProgressSink
from tests.test_agent_tool import _make_parent_agent, _make_registry


class _FakeApp:
    def __init__(self, agent):
        self.agent = agent


class _FakeAgent:
    def __init__(self, progress, agent_name=None):
        self._exec_ctx = ExecutionContext(cwd=Path("."), progress=progress)
        if agent_name is not None:
            self._agent_name = agent_name


# ── 汇点本身 ────────────────────────────────────────────────────────


def test_sink_empty_initially():
    assert ProgressSink().snapshot() is None


def test_sink_records_and_clears():
    sink = ProgressSink()
    sink.note("researcher", "调工具", "grep")
    note = sink.snapshot()
    assert note is not None
    assert (note.agent, note.action, note.detail) == ("researcher", "调工具", "grep")
    assert note.at > 0
    sink.clear()
    assert sink.snapshot() is None


def test_sink_keeps_only_the_latest():
    """只留最后一条 —— 它是给"卡住了吗"用的，不是审计日志。"""
    sink = ProgressSink()
    sink.note("a", "等模型响应")
    sink.note("b", "调工具", "bash")
    assert sink.snapshot().agent == "b"


# ── 看门狗文案 ──────────────────────────────────────────────────────


def test_hint_falls_back_to_model_when_nothing_recorded():
    from tui.app import _progress_hint

    assert _progress_hint(_FakeApp(_FakeAgent(ProgressSink()))) == "模型响应"


def test_hint_survives_missing_agent_or_sink():
    """app 上还没有 agent、或父没挂汇时都不能报错。"""
    from tui.app import _progress_hint

    assert _progress_hint(_FakeApp(None)) == "模型响应"
    assert _progress_hint(_FakeApp(_FakeAgent(None))) == "模型响应"


def test_hint_names_child_and_action():
    """核心：把「等模型响应」换成人话 —— 在等哪个子 Agent、它在做什么。"""
    from tui.app import _progress_hint

    sink = ProgressSink()
    sink.note("researcher", "调工具", "write_file")
    hint = _progress_hint(_FakeApp(_FakeAgent(sink)))
    assert "researcher" in hint
    assert "调工具" in hint
    assert "write_file" in hint
    assert "模型响应" not in hint, "跑子 Agent 时不该再说在等模型"


def test_hint_omits_empty_detail():
    from tui.app import _progress_hint

    sink = ProgressSink()
    sink.note("sub", "等模型响应")
    assert _progress_hint(_FakeApp(_FakeAgent(sink))) == "子 Agent「sub」（等模型响应）"


# ── 子 Agent 写进度 / 收尾清理 ──────────────────────────────────────


def test_progress_helper_writes_agent_name():
    from core.agent.sub_agent import _progress

    sink = ProgressSink()
    _progress(_FakeAgent(sink, agent_name="researcher"), "调工具", "grep")

    note = sink.snapshot()
    assert note is not None
    assert note.agent == "researcher"
    assert note.action == "调工具"


def test_progress_helper_defaults_agent_name():
    from core.agent.sub_agent import _progress

    sink = ProgressSink()
    _progress(_FakeAgent(sink), "等模型响应")

    assert sink.snapshot().agent == "sub"


def test_progress_helpers_are_noop_without_sink():
    """没挂汇时必须是 no-op（单测 / 无人值守路径不该因此报错）。"""
    from core.agent.sub_agent import _clear_progress, _progress

    agent = _FakeAgent(None)
    _progress(agent, "x")
    _clear_progress(agent)


def test_clear_progress_empties_the_sink():
    """子 Agent 收尾要清空，否则父回到等模型时界面还显示子 Agent 的旧状态。"""
    from core.agent.sub_agent import _clear_progress

    sink = ProgressSink()
    sink.note("sub", "调工具", "bash")
    _clear_progress(_FakeAgent(sink))
    assert sink.snapshot() is None


# ── 继承：子 Agent / 队友必须共用父的汇 ─────────────────────────────


def test_fork_subagent_inherits_parent_sink():
    """不继承的话界面读到的永远是空 —— 这个功能就白做了。"""
    from core.tool.tools.agent_tool import AgentTool

    parent = _make_parent_agent(_make_registry())
    sink = ProgressSink()
    parent._exec_ctx.progress = sink

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=False)
    tool.set_parent(parent)
    sub = tool._build_sub_agent(
        role=None, allowed=["write_file"], session_id="main", is_fork=True
    )

    assert sub._exec_ctx.progress is sink


def test_parent_without_sink_yields_child_without_sink():
    """父没挂汇 → 子也不挂：别在测试/无人值守路径上凭空造一个汇。"""
    from core.tool.tools.agent_tool import AgentTool

    parent = _make_parent_agent(_make_registry())
    assert parent._exec_ctx.progress is None

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=False)
    tool.set_parent(parent)
    sub = tool._build_sub_agent(
        role=None, allowed=["write_file"], session_id="main", is_fork=True
    )

    assert sub._exec_ctx.progress is None
