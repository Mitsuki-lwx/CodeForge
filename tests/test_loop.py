"""Agent 循环插件化（spec_loop）单测。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.loop import AgentLoop, ReactLoop, load_loop
from core.agent.role_loader import load_catalog
from core.tool.context import ExecutionContext
from core.tool.tools.agent_tool import AgentTool


class _Cfg:
    model = "x"
    context_window = 200000


class _Client:
    config = _Cfg()


class _EchoLoop(AgentLoop):
    """自定义 loop：不调工具，直接产文本。"""

    async def run(self, agent, conv, user_input):
        from core.agent.events import AgentFinished

        yield AgentFinished(
            text="echo", total_usage={}, iterations=1, elapsed_s=0.1
        )

    async def run_to_completion(self, agent, conv, task="", events=None):
        return "echo"


def _agent() -> Agent:
    return Agent(
        registry=__import__("core.tool.registry", fromlist=["ToolRegistry"]).ToolRegistry(),
        llm_client=_Client(),
        exec_ctx=ExecutionContext(cwd=Path(tempfile.mkdtemp()), session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
    )


# ── 默认 ReactLoop ────────────────────────────────────────────────

def test_default_loop_is_react():
    a = _agent()
    assert isinstance(a._loop, ReactLoop)


def test_set_loop_switches():
    a = _agent()
    a.set_loop(_EchoLoop())
    assert isinstance(a._loop, _EchoLoop)


# ── load_loop ─────────────────────────────────────────────────────

def test_load_loop_default_and_react():
    assert isinstance(load_loop(""), ReactLoop)
    assert isinstance(load_loop("react"), ReactLoop)


def test_load_loop_module_path(tmp_path):
    mod = tmp_path / "my_loop.py"
    mod.write_text(
        "from core.agent.loop import AgentLoop\n"
        "class L(AgentLoop):\n"
        "    async def run(self, agent, conv, user_input):\n"
        "        yield None\n"
        "    async def run_to_completion(self, agent, conv, task='', events=None):\n"
        "        return 'x'\n"
        "def create_loop(agent):\n"
        "    return L()\n",
        encoding="utf-8",
    )
    loop = load_loop(str(mod), agent=None)
    assert isinstance(loop, AgentLoop)
    assert not isinstance(loop, ReactLoop)  # 自定义生效


def test_load_loop_bad_path_falls_back():
    assert isinstance(load_loop("/nonexistent/loop.py"), ReactLoop)


def test_load_loop_module_without_loop_falls_back(tmp_path):
    mod = tmp_path / "empty_loop.py"
    mod.write_text("x = 1\n", encoding="utf-8")
    assert isinstance(load_loop(str(mod)), ReactLoop)


# ── 自定义 loop 生效 ──────────────────────────────────────────────

async def test_custom_loop_run_used():
    a = _agent()
    a.set_loop(_EchoLoop())
    seen = False
    async for ev in a.run("hi"):
        from core.agent.events import AgentFinished

        if isinstance(ev, AgentFinished) and ev.text == "echo":
            seen = True
    assert seen  # 自定义 loop 的 run 生效（替代 ReactLoop）


# ── 子 agent 继承 loop ────────────────────────────────────────────

def test_subagent_inherits_loop():
    a = _agent()
    a.set_loop(_EchoLoop())
    tool = AgentTool(catalog=load_catalog("."), task_mgr=None, bg_enabled=True)
    tool.set_parent(a)
    role = load_catalog(".").fork_role()
    allowed = [t.name() for t in a._registry.list()]
    sub = tool._build_sub_agent(role, allowed, "main", is_fork=True)
    assert sub._loop is a._loop


# ── 运行阶段状态机（idle/running）──────────────────────────────────

def test_phase_initial_idle():
    a = _agent()
    assert a.running is False
    assert a._phase == "idle"


def test_set_phase_manual():
    a = _agent()
    a.set_phase("running")
    assert a.running is True
    a.set_phase("idle")
    assert a.running is False


async def test_phase_running_during_run():
    a = _agent()
    a.set_loop(_EchoLoop())
    seen = []
    async for ev in a.run("hi"):
        seen.append(a.running)
    assert seen and all(seen)
    assert a.running is False


# ── pre_step 钩子（每轮 LLM 前拦截）───────────────────────────────

async def test_pre_step_blocked_stops_run(tmp_path):
    from core.agent.events import AgentError
    from core.hooks.runner import HookRunner
    from core.hooks.rules import HookAction, HookRule

    rule = HookRule(
        name="budget", event="pre_step",
        action=HookAction(type="command", command="exit 1"),
    )
    runner = HookRunner(rules=[rule], cwd=tmp_path)
    a = _agent()
    a._hooks = runner
    got = []
    async for ev in a.run("hi"):
        got.append(ev)
    err = [e for e in got if isinstance(e, AgentError)]
    assert err and err[0].code == "pre_step_blocked"


async def test_pre_step_pass_allows_run(tmp_path):
    from core.agent.events import AgentError, AgentFinished
    from core.hooks.runner import HookRunner
    from core.hooks.rules import HookAction, HookRule

    rule = HookRule(
        name="ok", event="pre_step",
        action=HookAction(type="command", command="exit 0"),
    )
    runner = HookRunner(rules=[rule], cwd=tmp_path)
    a = _agent()
    a.set_loop(_EchoLoop())
    a._hooks = runner
    got = []
    async for ev in a.run("hi"):
        got.append(ev)
    assert not any(isinstance(e, AgentError) for e in got)
    assert any(isinstance(e, AgentFinished) for e in got)


# ── turn 结束原因 + max-tokens sticky ─────────────────────────────

class _StopClient:
    """mock LLM：每轮返回指定 stop_reason。"""

    config = _Cfg()

    def __init__(self, stops):
        self._stops = list(stops)

    async def stream_chat(self, messages, system_prompt="", tools=None, system_blocks=None):
        from llm.stream_events import CompletionDone, TextChunk

        for sr in self._stops:
            yield TextChunk(text="ok")
            yield CompletionDone(usage={"input_tokens": 1, "output_tokens": 1}, stop_reason=sr)


def _agent_with(client):
    return Agent(
        registry=__import__("core.tool.registry", fromlist=["ToolRegistry"]).ToolRegistry(),
        llm_client=client,
        exec_ctx=ExecutionContext(cwd=Path(tempfile.mkdtemp()), session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=5),
    )


async def test_turn_end_completed_default():
    a = _agent_with(_StopClient(["end_turn"]))
    finished = None
    async for ev in a.run("hi"):
        from core.agent.events import AgentFinished

        if isinstance(ev, AgentFinished):
            finished = ev
    assert finished is not None
    assert finished.turn_end_reason == "completed"


async def test_turn_end_max_tokens():
    a = _agent_with(_StopClient(["max_tokens"]))
    finished = None
    async for ev in a.run("hi"):
        from core.agent.events import AgentFinished

        if isinstance(ev, AgentFinished):
            finished = ev
    assert finished is not None
    assert finished.turn_end_reason == "max-tokens"


async def test_pre_step_blocked_sets_reason(tmp_path):
    from core.agent.events import AgentError
    from core.hooks.runner import HookRunner
    from core.hooks.rules import HookAction, HookRule

    rule = HookRule(
        name="budget", event="pre_step",
        action=HookAction(type="command", command="exit 1"),
    )
    runner = HookRunner(rules=[rule], cwd=tmp_path)
    a = _agent_with(_StopClient(["end_turn"]))
    a._hooks = runner
    got = []
    async for ev in a.run("hi"):
        got.append(ev)
    err = [e for e in got if isinstance(e, AgentError)]
    assert err and err[0].code == "pre_step_blocked"
    assert a._turn_end_reason == "blocked"
