"""Agent 上下文压缩集成测试。

覆盖 runtime=None 退化、紧急压缩（PTL → 重建历史 → 重试）、手动 /compress、
ReadFile 追踪。这些路径在 runtime=None 的既有测试中不执行，故单独覆盖。
"""

from __future__ import annotations

from pathlib import Path

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent import Agent, AgentConfig
from core.agent.events import (
    AgentError,
    AgentFinished,
    CompactEvent,
    CompactPhase,
)
from core.agent.runtime import SessionRuntime
from core.context_compression.state import new_session_context
from core.permissions.modes import PermissionMode
from core.tool.context import ExecutionContext
from core.tool.registry import ToolRegistry
from core.tool.tools.read_file import ReadFileTool
from llm import PromptTooLongError
from llm.client import LLMClient
from llm.stream_events import CompletionDone, TextChunk

PROVIDER = ProviderConfig(
    name="mock", protocol="anthropic", model="mock", api_key="mock"
)


class PTLThenOkClient(LLMClient):
    """第一次主对话调用抛 PTL，之后返回指定文本。"""

    def __init__(self, answer: str = "done"):
        super().__init__(PROVIDER)
        self.calls = 0
        self.answer = answer

    async def stream_chat(
        self, messages, system_prompt="", tools=None, system_blocks=None
    ):
        self.calls += 1
        if self.calls == 1:
            raise PromptTooLongError("prompt is too long")
        yield TextChunk(text=self.answer)
        yield CompletionDone()


class SummaryClient(LLMClient):
    """摘要请求客户端（经 LLMClient.create 注入）。"""

    def __init__(self):
        super().__init__(PROVIDER)
        self.calls = 0

    async def stream_chat(
        self, messages, system_prompt="", tools=None, system_blocks=None
    ):
        self.calls += 1
        yield TextChunk(text="<summary>SUMMARY_TEXT</summary>")
        yield CompletionDone()


def _make_agent(tmp_path: Path, client, runtime: SessionRuntime | None):
    reg = ToolRegistry()
    reg.register(ReadFileTool())
    exec_ctx = ExecutionContext(cwd=tmp_path, session_id="test")
    conversation = ConversationManager(system_prompt="")
    agent = Agent(
        registry=reg,
        llm_client=client,
        exec_ctx=exec_ctx,
        conversation=conversation,
        config=AgentConfig(max_iterations=5),
        runtime=runtime,
    )
    agent.set_permission_mode(PermissionMode.BYPASS)
    return agent, conversation


def _runtime(tmp_path: Path) -> SessionRuntime:
    return SessionRuntime(
        session=new_session_context(str(tmp_path)),
        context_window=200000,
    )


async def test_emergency_compact_recovers(tmp_path, monkeypatch):
    client = PTLThenOkClient(answer="final answer")
    summary = SummaryClient()
    monkeypatch.setattr(LLMClient, "create", lambda cfg: summary)
    agent, _ = _make_agent(tmp_path, client, _runtime(tmp_path))

    events = [ev async for ev in agent.run("hello")]
    phases = [ev.phase for ev in events if isinstance(ev, CompactEvent)]
    assert CompactPhase.BEFORE_EMERGENCY in phases
    assert CompactPhase.AFTER_EMERGENCY in phases
    assert any(isinstance(ev, AgentFinished) for ev in events)
    assert client.calls == 2  # 第一次 PTL + 紧急压缩后重试
    assert agent._runtime.usage_anchor == 0  # 紧急压缩后锚点清零


async def test_runtime_none_degrades(tmp_path):
    client = PTLThenOkClient(answer="done")
    agent, _ = _make_agent(tmp_path, client, None)
    events = [ev async for ev in agent.run("hello")]
    assert any(isinstance(ev, AgentError) and ev.code == "ptl_error" for ev in events)
    assert client.calls == 1  # 无 runtime：不重试
    assert not any(isinstance(ev, CompactEvent) for ev in events)


async def test_run_force_compact_manual(tmp_path, monkeypatch):
    summary = SummaryClient()
    monkeypatch.setattr(LLMClient, "create", lambda cfg: summary)
    agent, conv = _make_agent(tmp_path, PTLThenOkClient(), _runtime(tmp_path))
    # 交替播种 15 条 ~4KB 消息，使压缩前 token 远大于压缩后
    conv.add_user_message("x" * 4000)
    for _ in range(7):
        conv.add_assistant_message("y" * 4000)
        conv.add_user_message("x" * 4000)

    before, after = await agent.run_force_compact()
    assert before > after
    assert "SUMMARY_TEXT" in conv.messages[0].content
    assert summary.calls == 1


async def test_read_file_tracking(tmp_path):
    runtime = _runtime(tmp_path)
    agent, _ = _make_agent(tmp_path, PTLThenOkClient(), runtime)
    target = tmp_path / "sample.py"
    target.write_bytes(b"print('hello')\n")

    await agent._track_read_file({"file_path": str(target)})
    snapshot = runtime.recovery.snapshot()
    assert snapshot[0].path == str(target.resolve())
    assert snapshot[0].content == "print('hello')\n"


async def test_read_file_tracking_relative_path(tmp_path):
    runtime = _runtime(tmp_path)
    agent, _ = _make_agent(tmp_path, PTLThenOkClient(), runtime)
    (tmp_path / "rel.txt").write_text("data", encoding="utf-8")

    await agent._track_read_file({"file_path": "rel.txt"})
    snapshot = runtime.recovery.snapshot()
    assert snapshot[0].path == str((tmp_path / "rel.txt").resolve())


# ── 自动笔记触发（F35/F36）────────────────────────────────────────


async def test_memory_trigger_creates_note(tmp_path, monkeypatch):
    from core.notes import NoteStore
    from llm.client import LLMClient
    from llm.stream_events import CompletionDone, TextChunk

    class _FakeMemo(LLMClient):
        async def stream_chat(
            self, messages, system_prompt="", tools=None, system_blocks=None
        ):
            yield TextChunk(
                text='[{"action":"create","level":"user","type":"user_preference",'
                '"title":"简洁回复","slug":"terse","content":"回复要简洁"}]'
            )
            yield CompletionDone()

    monkeypatch.setattr(LLMClient, "create", lambda cfg: _FakeMemo(PROVIDER))

    notes = NoteStore(tmp_path, user_home=tmp_path / "home")
    runtime = _runtime(tmp_path)
    runtime.notes = notes
    client = PTLThenOkClient(answer="done")
    agent, _ = _make_agent(tmp_path, client, runtime)

    events = [ev async for ev in agent.run("请记住：回复要简洁")]
    assert any(isinstance(ev, AgentFinished) for ev in events)
    assert runtime.turn_count == 1
    await agent.shutdown_memory(timeout=2.0)

    files = [f for f in notes._user_dir.glob("*.md") if f.name != "MEMORY.md"]
    assert any("user_preference" in f.name for f in files)


async def test_memory_not_triggered_without_runtime(tmp_path):
    class _OkClient(LLMClient):
        def __init__(self):
            super().__init__(PROVIDER)

        async def stream_chat(
            self, messages, system_prompt="", tools=None, system_blocks=None
        ):
            yield TextChunk(text="done")
            yield CompletionDone()

    runtime = _runtime(tmp_path)  # notes=None
    client = _OkClient()
    agent, _ = _make_agent(tmp_path, client, runtime)
    events = [ev async for ev in agent.run("请记住：重要")]
    assert any(isinstance(ev, AgentFinished) for ev in events)
    assert runtime.turn_count == 0  # 未启用笔记则不计数
    assert agent._memory_tasks == set()


def test_new_commands_registered():
    from core.commands import Registry, register_builtins

    reg = Registry()
    register_builtins(reg)
    for cmd in ("resume", "notes", "session", "memory"):
        assert reg.lookup(cmd) is not None
    assert reg.lookup("/RESUME") is not None  # 大小写/斜杠不敏感
    # /review 已从内置命令移除 —— 由 Skill 系统提供
    assert reg.lookup("review") is None
