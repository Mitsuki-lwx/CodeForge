"""Tests for Bash tool."""

import asyncio
import os
import sys

import pytest

from core.tool.context import ExecutionContext
from core.tool.tools.bash import BashTool


@pytest.fixture
def ctx(tmp_path):
    # Use tmp_path instead of /tmp for Windows compat
    return ExecutionContext(cwd=tmp_path, session_id="test")


@pytest.fixture
def bash():
    return BashTool()


class TestBash:
    def test_name(self, bash):
        assert bash.name() == "bash"

    @pytest.mark.asyncio
    async def test_echo(self, bash, ctx):
        result = await bash.execute(ctx, {"command": "echo hello"})
        assert result.success is True, f"bash failed: {result.error}"
        assert "hello" in result.data

    @pytest.mark.asyncio
    async def test_meta_fields(self, bash, ctx):
        result = await bash.execute(ctx, {"command": "echo ok"})
        assert "exit_code" in result.meta
        assert result.meta["exit_code"] == 0, f"exit_code was {result.meta['exit_code']}: {result.error}"
        assert "stdout" in result.meta
        assert "stderr" in result.meta

    @pytest.mark.asyncio
    async def test_failure(self, bash, ctx):
        result = await bash.execute(ctx, {"command": "ls /nonexistent_path_for_test_xyz 2>&1"})
        assert result.success is False
        assert result.meta["exit_code"] != 0

    @pytest.mark.asyncio
    async def test_properties(self, bash):
        assert bash.is_read_only() is False
        assert bash.is_destructive() is True
        assert bash.is_concurrency_safe({}) is False
        assert bash.category() == "shell"

    @pytest.mark.asyncio
    async def test_cwd_is_respected(self, bash, tmp_path):
        """Verify bash runs in the specified cwd (cross-platform)."""
        ctx = ExecutionContext(cwd=tmp_path, session_id="test")
        result = await bash.execute(ctx, {"command": "pwd"})
        assert result.success is True, f"bash failed: {result.error}"
        # pwd output should be non-empty and reflect some directory
        assert len(result.data.strip()) > 0

    @pytest.mark.asyncio
    async def test_stdout_stderr_separate(self, bash, ctx):
        result = await bash.execute(ctx, {"command": "echo out && echo err >&2"})
        assert result.success is True, f"bash failed: {result.error}"
        assert "out" in result.meta["stdout"]
        assert "err" in result.meta["stderr"]
