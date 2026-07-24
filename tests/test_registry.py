"""Tests for ToolRegistry."""

import asyncio

import pytest

from core.tool.context import ExecutionContext
from core.tool.errors import ToolNotFoundError
from core.tool.interface import Tool
from core.tool.registry import ToolRegistry
from core.tool.result import ToolResult
from tests.test_tool_interface import _ConcreteTool


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def ctx():
    return ExecutionContext(cwd="/tmp", session_id="test-session")


class TestRegistration:
    def test_register_and_get(self, registry):
        tool = _ConcreteTool()
        registry.register(tool)
        assert registry.get("test_tool") is tool

    def test_get_not_found(self, registry):
        with pytest.raises(ToolNotFoundError, match="not_exist"):
            registry.get("not_exist")

    def test_list_empty(self, registry):
        assert registry.list() == []

    def test_list_after_register(self, registry):
        registry.register(_ConcreteTool())
        names = [t.name() for t in registry.list()]
        assert names == ["test_tool"]

    def test_register_overwrite(self, registry):
        t1 = _ConcreteTool()
        registry.register(t1)
        t2 = _ConcreteTool()
        registry.register(t2)
        assert registry.get("test_tool") is t2


class TestExecute:
    @pytest.mark.asyncio
    async def test_execute_success(self, registry, ctx):
        registry.register(_ConcreteTool())
        result = await registry.execute("test_tool", ctx, {"name": "World"})
        assert result.success is True
        assert result.data == "Hello World"

    @pytest.mark.asyncio
    async def test_execute_not_found(self, registry, ctx):
        result = await registry.execute("unknown", ctx, {})
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_validation_error(self, registry, ctx):
        registry.register(_ConcreteTool())
        result = await registry.execute("test_tool", ctx, {})
        assert result.success is False
        assert "validation" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_meta_includes_tool_name(self, registry, ctx):
        registry.register(_ConcreteTool())
        result = await registry.execute("test_tool", ctx, {"name": "x"})
        assert result.meta.get("tool") == "test_tool"


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self, registry, ctx):
        class SlowTool(_ConcreteTool):
            timeout_seconds = 0.1

            async def execute(self, context, input):
                await asyncio.sleep(10)
                return ToolResult(success=True)

        registry.register(SlowTool())
        result = await registry.execute("test_tool", ctx, {"name": "x"})
        assert result.success is False
        assert "timed out" in result.error.lower()


class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, registry, ctx):
        call_count = 0

        class FlakyTool(_ConcreteTool):
            max_retries = 2

            async def execute(self, context, input):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise OSError("transient IO error")
                return ToolResult(success=True, data="OK after retry")

        registry.register(FlakyTool())
        result = await registry.execute("test_tool", ctx, {"name": "x"})
        assert result.success is True
        assert result.data == "OK after retry"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_returns_failure(self, registry, ctx):
        class AlwaysFailTool(_ConcreteTool):
            max_retries = 1

            async def execute(self, context, input):
                raise OSError("persistent error")

        registry.register(AlwaysFailTool())
        result = await registry.execute("test_tool", ctx, {"name": "x"})
        assert result.success is False
        assert "persistent" in result.error


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrency_safe_tools_run_in_parallel(self):
        """ReadOnly tools should not block each other."""
        import time

        class SlowReadTool(_ConcreteTool):
            def is_concurrency_safe(self, input):
                return True

            async def execute(self, context, input):
                await asyncio.sleep(0.2)
                return ToolResult(success=True, data="done")

        reg = ToolRegistry()
        reg.register(SlowReadTool())
        ctx = ExecutionContext(cwd="/tmp")

        t0 = time.monotonic()
        results = await asyncio.gather(
            reg.execute("test_tool", ctx, {"name": "a"}),
            reg.execute("test_tool", ctx, {"name": "b"}),
        )
        elapsed = time.monotonic() - t0
        assert all(r.success for r in results)
        # If parallel, should take ~0.2s not ~0.4s
        assert elapsed < 0.35

    @pytest.mark.asyncio
    async def test_not_concurrency_safe_serialized(self):
        """Write tools to the same path should be serialized."""
        import time

        class SlowWriteTool(_ConcreteTool):
            def is_concurrency_safe(self, input):
                return False

            async def execute(self, context, input):
                await asyncio.sleep(0.2)
                return ToolResult(success=True, data=input.get("name"))

        reg = ToolRegistry()
        reg.register(SlowWriteTool())
        ctx = ExecutionContext(cwd="/tmp")

        t0 = time.monotonic()
        results = await asyncio.gather(
            reg.execute("test_tool", ctx, {"name": "a", "file_path": "/tmp/x"}),
            reg.execute("test_tool", ctx, {"name": "b", "file_path": "/tmp/x"}),
        )
        elapsed = time.monotonic() - t0
        assert all(r.success for r in results)
        # Serialized so should take ~0.4s
        assert elapsed >= 0.35

    @pytest.mark.asyncio
    async def test_different_paths_not_serialized(self):
        """Write tools to different paths should NOT block each other."""
        import time

        class SlowWriteTool(_ConcreteTool):
            def is_concurrency_safe(self, input):
                return False

            async def execute(self, context, input):
                await asyncio.sleep(0.2)
                return ToolResult(success=True, data=input.get("name"))

        reg = ToolRegistry()
        reg.register(SlowWriteTool())
        ctx = ExecutionContext(cwd="/tmp")

        t0 = time.monotonic()
        results = await asyncio.gather(
            reg.execute("test_tool", ctx, {"name": "a", "file_path": "/tmp/a"}),
            reg.execute("test_tool", ctx, {"name": "b", "file_path": "/tmp/b"}),
        )
        elapsed = time.monotonic() - t0
        assert all(r.success for r in results)
        # Different paths -> parallel -> ~0.2s
        assert elapsed < 0.35
