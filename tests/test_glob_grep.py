"""Tests for Glob and Grep tools."""

import tempfile
from pathlib import Path

import pytest

from core.tool.context import ExecutionContext
from core.tool.tools.glob_tool import GlobTool
from core.tool.tools.grep_tool import GrepTool


@pytest.fixture
def tmp_root():
    with tempfile.TemporaryDirectory(prefix="codeforge_test_") as d:
        yield Path(d)


@pytest.fixture
def ctx(tmp_root):
    return ExecutionContext(cwd=tmp_root, session_id="test")


@pytest.fixture
def glob_tool():
    return GlobTool()


@pytest.fixture
def grep_tool():
    return GrepTool()


class TestGlob:
    def test_name(self, glob_tool):
        assert glob_tool.name() == "glob"

    @pytest.mark.asyncio
    async def test_glob_empty_dir(self, glob_tool, ctx, tmp_root):
        result = await glob_tool.execute(ctx, {"pattern": "*.txt"})
        assert result.success is True
        assert result.data == []

    @pytest.mark.asyncio
    async def test_glob_finds_files(self, glob_tool, ctx, tmp_root):
        (tmp_root / "a.py").write_text("")
        (tmp_root / "b.py").write_text("")
        (tmp_root / "c.txt").write_text("")
        result = await glob_tool.execute(ctx, {"pattern": "*.py"})
        assert result.success is True
        assert set(result.data) == {"a.py", "b.py"}

    @pytest.mark.asyncio
    async def test_glob_nested(self, glob_tool, ctx, tmp_root):
        sub = tmp_root / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("")
        result = await glob_tool.execute(ctx, {"pattern": "**/*.py"})
        assert result.success is True
        assert any("deep.py" in p for p in result.data)

    @pytest.mark.asyncio
    async def test_glob_with_path(self, glob_tool, ctx, tmp_root):
        sub = tmp_root / "sub"
        sub.mkdir()
        (sub / "inside.py").write_text("")
        (tmp_root / "outside.py").write_text("")
        result = await glob_tool.execute(ctx, {"pattern": "*.py", "path": str(sub)})
        assert result.success is True
        assert result.data == ["inside.py"]

    @pytest.mark.asyncio
    async def test_meta(self, glob_tool, ctx, tmp_root):
        (tmp_root / "x.py").write_text("")
        result = await glob_tool.execute(ctx, {"pattern": "*.py"})
        assert result.meta["count"] == 1
        assert result.meta["pattern"] == "*.py"

    def test_properties(self, glob_tool):
        assert glob_tool.is_read_only() is True
        assert glob_tool.is_destructive() is False
        assert glob_tool.is_concurrency_safe({}) is True
        assert glob_tool.category() == "code_search"


class TestGrep:
    def test_name(self, grep_tool):
        assert grep_tool.name() == "grep"

    @pytest.mark.asyncio
    async def test_grep_basic(self, grep_tool, ctx, tmp_root):
        f = tmp_root / "data.txt"
        f.write_text("hello world\nfoo bar\nbaz hello\n", encoding="utf-8")
        result = await grep_tool.execute(ctx, {"pattern": "hello"})
        assert result.success is True
        assert len(result.data) == 2
        assert result.data[0]["line"] == 1
        assert result.data[1]["line"] == 3

    @pytest.mark.asyncio
    async def test_grep_no_match(self, grep_tool, ctx, tmp_root):
        f = tmp_root / "data.txt"
        f.write_text("aaa bbb ccc", encoding="utf-8")
        result = await grep_tool.execute(ctx, {"pattern": "zzz"})
        assert result.success is True
        assert result.data == []

    @pytest.mark.asyncio
    async def test_grep_files_with_matches(self, grep_tool, ctx, tmp_root):
        (tmp_root / "a.txt").write_text("hello", encoding="utf-8")
        (tmp_root / "b.txt").write_text("world", encoding="utf-8")
        (tmp_root / "c.txt").write_text("hello again", encoding="utf-8")
        result = await grep_tool.execute(ctx, {"pattern": "hello", "output_mode": "files_with_matches"})
        assert result.success is True
        assert set(result.data) == {"a.txt", "c.txt"}

    @pytest.mark.asyncio
    async def test_grep_count(self, grep_tool, ctx, tmp_root):
        (tmp_root / "a.txt").write_text("hello\nhello\nbye", encoding="utf-8")
        (tmp_root / "b.txt").write_text("hello\nbye", encoding="utf-8")
        result = await grep_tool.execute(ctx, {"pattern": "hello", "output_mode": "count"})
        assert result.success is True
        assert result.data == 3

    @pytest.mark.asyncio
    async def test_grep_include(self, grep_tool, ctx, tmp_root):
        (tmp_root / "match.py").write_text("hello", encoding="utf-8")
        (tmp_root / "skip.txt").write_text("hello", encoding="utf-8")
        result = await grep_tool.execute(ctx, {"pattern": "hello", "include": "*.py"})
        assert result.success is True
        assert len(result.data) == 1
        assert "match.py" in result.data[0]["file"]

    def test_properties(self, grep_tool):
        assert grep_tool.is_read_only() is True
        assert grep_tool.is_destructive() is False
        assert grep_tool.is_concurrency_safe({}) is True
        assert grep_tool.category() == "code_search"
