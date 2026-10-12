"""Tests for Bash tool."""

import os
import shutil
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
        assert result.meta["exit_code"] == 0, (
            f"exit_code was {result.meta['exit_code']}: {result.error}"
        )
        assert "stdout" in result.meta
        assert "stderr" in result.meta

    @pytest.mark.asyncio
    async def test_failure(self, bash, ctx):
        result = await bash.execute(
            ctx, {"command": "ls /nonexistent_path_for_test_xyz 2>&1"}
        )
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
        """Verify bash runs in the specified cwd (cross-platform).

        不用 `pwd` —— 那是 Unix 工具，Windows 上要靠 Git 的 `usr\\bin` 才在 PATH 里，
        CI 的 windows runner 没有它。改用解释器自身打印 cwd：两边都成立，
        而且断言更强（真的比对了目录，而不是"输出非空"）。
        """
        ctx = ExecutionContext(cwd=tmp_path, session_id="test")
        result = await bash.execute(
            ctx, {"command": f'"{sys.executable}" -c "import os;print(os.getcwd())"'}
        )
        assert result.success is True, f"bash failed: {result.error}"
        assert os.path.samefile(result.data.strip(), tmp_path)

    @pytest.mark.asyncio
    async def test_stdout_stderr_separate(self, bash, ctx):
        result = await bash.execute(ctx, {"command": "echo out && echo err >&2"})
        assert result.success is True, f"bash failed: {result.error}"
        assert "out" in result.meta["stdout"]
        assert "err" in result.meta["stderr"]

    @pytest.mark.skipif(
        shutil.which("grep") is None, reason="本机没有 grep（Unix 工具），跳过"
    )
    @pytest.mark.asyncio
    async def test_grep_no_match_non_error(self, bash, ctx, tmp_path):
        """grep 退出码 1(无匹配)不算错误,success=True 且附语义提示。"""
        ctx2 = ExecutionContext(cwd=tmp_path, session_id="t")
        # 用管道：printf/echo 的输出里没有 zzz → grep 退出 1（无匹配，不是错误）
        result = await bash.execute(ctx2, {"command": "echo abc | grep -q zzz"})
        assert result.success is True, f"grep exit1 should be non-error: {result}"
        assert "no matches found" in result.data

    @pytest.mark.asyncio
    async def test_ordinary_nonzero_is_error(self, bash, ctx):
        """普通命令(如 ls 不存在的目录)非零退出 → 判为错误。"""
        result = await bash.execute(ctx, {"command": "ls /nonexistent_dir_xyzabc 2>&1"})
        assert result.success is False
        assert result.meta["exit_code"] != 0
