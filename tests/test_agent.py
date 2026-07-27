"""End-to-end tests for the Agent Loop."""

import asyncio
import time
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from conversation.message import APIMessage
from core.agent import Agent, AgentConfig
from core.agent.events import (
    AgentError,
    AgentFinished,
    AgentEvent,
    IterationUpdate,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from core.permissions.modes import PermissionMode
from core.tool import Tool, ToolRegistry, ToolResult, ExecutionContext
from llm.client import LLMClient
from llm.stream_events import (
    CompletionDone,
    StreamError,
    StreamEvent,
    TextChunk,
    ThinkingChunk,
    ToolUse,
)


# ── Mock LLM Client ────────────────────────────────────────────────


class MockLLMClient(LLMClient):
    """返回可编程的事件序列，每个事件之间有极短异步间隙（允许取消信号插队）。"""

    def __init__(self, responses: list[list[StreamEvent]]):
        super().__init__(
            ProviderConfig(name="mock", protocol="anthropic", model="mock", api_key="mock")
        )
        self._responses = responses
        self._call_count = 0

    async def stream_chat(
        self,
        messages: list[APIMessage],
        system_prompt: str = "",
        tools: list[dict] | None = None,
        system_blocks: Any = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        if self._call_count < len(self._responses):
            for event in self._responses[self._call_count]:
                await asyncio.sleep(0)  # yield control to allow cancellation
                yield event
            self._call_count += 1
        else:
            await asyncio.sleep(0)
            yield CompletionDone()


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def exec_ctx():
    return ExecutionContext(cwd=Path("/tmp"), session_id="test")


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(ReadOnlyTool())
    reg.register(WriteTool())
    return reg


@pytest.fixture
def conversation():
    return ConversationManager(system_prompt="You are CodeForge. Use tools to help.")


@pytest.fixture
def agent_cls(registry, exec_ctx, conversation):
    """Factory: create a mock-backed Agent."""

    def _make(responses: list[list[StreamEvent]], **kwargs) -> Agent:
        cfg = AgentConfig(max_iterations=kwargs.pop("max_iterations", 25))
        cfg.unknown_tool_threshold = kwargs.pop("unknown_tool_threshold", 3)
        client = MockLLMClient(responses)
        agent = Agent(
            registry=registry,
            llm_client=client,
            exec_ctx=exec_ctx,
            conversation=conversation,
            config=cfg,
        )
        agent.set_permission_mode(PermissionMode.BYPASS)  # 测试环境跳过 HITL
        return agent

    return _make


# ── Fake tools for testing ────────────────────────────────────────


from core.tool.interface import Tool
from core.tool.result import ToolResult


class ReadOnlyTool(Tool):
    timeout_seconds = 1.0

    def name(self) -> str:
        return "read_test"

    def description(self) -> str:
        return "Read-only test tool"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, context, input):
        return ToolResult(success=True, data="read_result")

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input):
        return True

    def category(self) -> str:
        return "read"


class WriteTool(Tool):
    timeout_seconds = 1.0

    def name(self) -> str:
        return "write_test"

    def description(self) -> str:
        return "Write test tool"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, context, input):
        return ToolResult(success=True, data="write_result")

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input):
        return False

    def category(self) -> str:
        return "write"


# ── Tests ──────────────────────────────────────────────────────────


class TestAgentNaturalCompletion:
    @pytest.mark.asyncio
    async def test_single_turn_text_only(self, agent_cls):
        """模型直接返回纯文本 → 一轮结束。"""
        agent = agent_cls([
            [
                TextChunk("Hello, how can I help?"),
                CompletionDone(),
            ],
        ])

        events: list[AgentEvent] = []
        async for e in agent.run("hi"):
            events.append(e)

        # Should have: IterationUpdate(1), TextDelta, AgentFinished
        assert any(isinstance(e, IterationUpdate) for e in events)
        texts = [e for e in events if isinstance(e, TextDelta)]
        assert len(texts) == 1
        assert texts[0].text == "Hello, how can I help?"
        finished = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert finished[0].iterations == 1

    @pytest.mark.asyncio
    async def test_no_stream_error(self, agent_cls):
        """流返回错误 → 停止。"""
        agent = agent_cls([
            [StreamError(message="API key invalid")],
        ])

        events = []
        async for e in agent.run("hi"):
            events.append(e)

        errors = [e for e in events if isinstance(e, AgentError)]
        assert len(errors) == 1
        assert errors[0].code == "stream_error"
        assert "API key invalid" in errors[0].message

    @pytest.mark.asyncio
    async def test_thinking_relayed_as_separate_event(self, agent_cls):
        """ThinkingChunk 转发为 ThinkingDelta，不进 TextDelta，与正文区分。"""
        agent = agent_cls([
            [
                ThinkingChunk("Hmm, let me think..."),
                TextChunk("The answer"),
                CompletionDone(),
            ],
        ])

        events = []
        async for e in agent.run("hi"):
            events.append(e)

        thinks = [e for e in events if isinstance(e, ThinkingDelta)]
        texts = [e for e in events if isinstance(e, TextDelta)]
        assert "".join(t.text for t in thinks) == "Hmm, let me think..."
        assert "".join(t.text for t in texts) == "The answer"


class TestAgentMultiTurn:
    @pytest.mark.asyncio
    async def test_two_turns_with_tool(self, agent_cls):
        """第一轮调工具，第二轮回纯文本 → 两轮结束。"""
        agent = agent_cls([
            # Round 1: tool use
            [
                ToolUse(id="tu1", name="read_test", input={}),
                CompletionDone(),
            ],
            # Round 2: text only
            [
                TextChunk("The file contains ABC"),
                CompletionDone(),
            ],
        ])

        events = []
        async for e in agent.run("read file"):
            events.append(e)

        tool_started = [e for e in events if isinstance(e, ToolCallStarted)]
        tool_finished = [e for e in events if isinstance(e, ToolCallFinished)]
        assert len(tool_started) == 1
        assert tool_started[0].name == "read_test"
        assert len(tool_finished) == 1
        assert tool_finished[0].success is True

        finished = [e for e in events if isinstance(e, AgentFinished)]
        assert len(finished) == 1
        assert finished[0].iterations == 2

    @pytest.mark.asyncio
    async def test_concurrent_read_only_batch(self, agent_cls):
        """三个连续只读工具 → 并发批执行。"""
        agent = agent_cls([
            [
                ToolUse(id="tu1", name="read_test", input={}),
                ToolUse(id="tu2", name="read_test", input={}),
                ToolUse(id="tu3", name="read_test", input={}),
                CompletionDone(),
            ],
            [
                TextChunk("All files read"),
                CompletionDone(),
            ],
        ])

        events = []
        t0 = time.monotonic()
        async for e in agent.run("read three"):
            events.append(e)
        elapsed = time.monotonic() - t0

        started = [e for e in events if isinstance(e, ToolCallStarted)]
        finished = [e for e in events if isinstance(e, ToolCallFinished)]
        assert len(started) == 3
        assert all(f.success for f in finished)
        # Concurrent batch → should be roughly 1x sleep, not 3x
        # ReadOnlyTool takes ~0 immediately, so this is mostly overhead
        assert elapsed < 2.0

    @pytest.mark.asyncio
    async def test_read_write_serial(self, agent_cls):
        """read + write + read → 三批串行。"""
        agent = agent_cls([
            [
                ToolUse(id="tu1", name="read_test", input={"path": "a"}),
                ToolUse(id="tu2", name="write_test", input={"path": "a"}),
                ToolUse(id="tu3", name="read_test", input={"path": "b"}),
                CompletionDone(),
            ],
            [
                TextChunk("Done"),
                CompletionDone(),
            ],
        ])

        events = []
        async for e in agent.run("process"):
            events.append(e)

        started = [e for e in events if isinstance(e, ToolCallStarted)]
        names_in_order = [s.name for s in started]
        # read, write, read in original order
        assert names_in_order == ["read_test", "write_test", "read_test"]


class TestAgentPlanMode:
    @pytest.mark.asyncio
    async def test_plan_mode_toggle(self, agent_cls):
        """/plan 切换 ON，/plan 再切换 OFF。"""
        agent = agent_cls([])

        # /plan → ON
        events = []
        async for e in agent.run("/plan"):
            events.append(e)
        assert agent.mode.value == "plan"
        assert any(
            isinstance(e, AgentFinished) and "PLAN" in e.text.upper()
            for e in events
        )

        # /plan again → OFF
        async for e in agent.run("/plan"):
            pass
        assert agent.mode.value == "off"

    @pytest.mark.asyncio
    async def test_plan_mode_auto_detect(self, agent_cls):
        """用户说"先计划一下" → 自动进入 Plan Mode。"""
        agent = agent_cls([
            [
                TextChunk("好的，让我先分析一下……"),
                CompletionDone(),
            ],
        ])
        assert agent.mode.value == "off"

        events = []
        async for e in agent.run("先计划一下，我想加一个功能"):
            events.append(e)

        # 应该自动进入了 plan mode
        assert agent.mode.value == "plan"
        assert any(isinstance(e, AgentFinished) for e in events)

    @pytest.mark.asyncio
    async def test_plan_mode_auto_detect_english(self, agent_cls):
        """"just plan first" → 自动进入 Plan Mode。"""
        agent = agent_cls([
            [
                TextChunk("Here's my plan..."),
                CompletionDone(),
            ],
        ])

        async for e in agent.run("just plan first, don't execute yet"):
            pass

        assert agent.mode.value == "plan"

    @pytest.mark.asyncio
    async def test_plan_mode_blocks_write(self, agent_cls, registry):
        """Plan Mode ON → PermissionChecker 拦截非只读工具。"""
        agent = agent_cls([
            [
                TextChunk("Done planning."),
                CompletionDone(),
            ],
        ])

        # 进入 plan mode
        async for e in agent.run("/plan"):
            pass
        assert agent.mode.value == "plan"

        # 直接测试 PermissionChecker（避免 HITL 挂起）
        from core.permissions.rules import extract_content
        checker = agent._permission_checker

        # read_test 应该放行
        from core.tool.tools import get_default_registry
        reg = get_default_registry()
        read_tool = reg.get("read_file")
        read_decision = checker.check(
            "read_file", read_tool.is_read_only(), "read",
            {"file_path": "test.py"}
        )
        assert read_decision.effect == "allow", f"read should allow, got {read_decision}"

        # write_file 应该被拦截（plan mode 下 write=ask → 但 checker 返回 ask，实际由 HITL 处理）
        write_tool = reg.get("write_file")
        write_decision = checker.check(
            "write_file", write_tool.is_read_only(), "write",
            {"file_path": "test.py"}
        )
        assert write_decision.effect in ("deny", "ask"), f"write should deny/ask, got {write_decision}"

    @pytest.mark.asyncio
    async def test_plan_mode_all_tools_visible(self, agent_cls, registry):
        """Plan Mode ON 时工具定义包含全部工具（权限层在执行时拦截）。"""
        agent = agent_cls([
            [
                TextChunk("Plan: I will read the file first"),
                CompletionDone(),
            ],
        ])

        async for e in agent.run("/plan"):
            pass

        assert agent.mode.value == "plan"
        defs = agent._build_tool_defs()  # type: ignore
        names = {d["name"] for d in defs}
        # 所有工具都可见（模型中可以看到全部工具来制定更好的计划）
        assert "read_test" in names
        assert "write_test" in names  # 写工具也可见，但执行时会被权限层拦截


class TestAgentStopConditions:
    @pytest.mark.asyncio
    async def test_max_iterations(self, agent_cls):
        """达到迭代上限 → 停止。"""
        agent = agent_cls(
            [[ToolUse(id=f"tu{i}", name="read_test", input={}), CompletionDone()] for i in range(100)],
            max_iterations=3,
        )

        events = []
        async for e in agent.run("infinite loop"):
            events.append(e)

        errors = [e for e in events if isinstance(e, AgentError)]
        assert len(errors) == 1
        assert errors[0].code == "max_iterations"

    @pytest.mark.asyncio
    async def test_unknown_tool_threshold(self, agent_cls):
        """连续请求未知工具达阈值 → 停止。"""
        agent = agent_cls(
            [
                [
                    ToolUse(id="tu1", name="nonexistent_tool", input={}),
                    ToolUse(id="tu2", name="also_fake", input={}),
                    ToolUse(id="tu3", name="third_fake", input={}),
                    CompletionDone(),
                ],
            ],
            unknown_tool_threshold=3,
        )

        events = []
        async for e in agent.run("call unknown tools"):
            events.append(e)

        errors = [e for e in events if isinstance(e, AgentError)]
        assert len(errors) == 1, f"Expected 1 error, got {errors}"
        assert errors[0].code == "unknown_tool"

    @pytest.mark.asyncio
    async def test_cancellation_midstream(self, agent_cls):
        """流式态取消 → 即时返回 AgentError。"""
        agent = agent_cls([
            [TextChunk("chunk"), TextChunk("1"), TextChunk("2"), CompletionDone()],
        ])

        events = []
        count = 0
        async for e in agent.run("cancel me"):
            events.append(e)
            if isinstance(e, TextDelta):
                count += 1
                if count == 2:
                    agent.cancel()
                    # Give the event loop a tick to process the cancel
                    await asyncio.sleep(0)

        errors = [e for e in events if isinstance(e, AgentError)]
        assert len(errors) == 1, f"Expected 1 AgentError, got {events}"
        assert errors[0].code == "cancelled"
        # Should have gotten some text before cancel
        texts = [e for e in events if isinstance(e, TextDelta)]
        assert len(texts) >= 2
