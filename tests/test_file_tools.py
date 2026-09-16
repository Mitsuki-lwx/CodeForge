"""Tests for ReadFile, WriteFile, EditFile tools."""

import tempfile
from pathlib import Path

import pytest

from core.tool.context import ExecutionContext
from core.tool.tools.edit_file import EditFileTool
from core.tool.tools.read_file import ReadFileTool
from core.tool.tools.write_file import WriteFileTool


@pytest.fixture
def tmp_root():
    with tempfile.TemporaryDirectory(prefix="codeforge_test_") as d:
        yield Path(d)


@pytest.fixture
def ctx(tmp_root):
    return ExecutionContext(cwd=tmp_root, session_id="test")


@pytest.fixture
def read_file():
    return ReadFileTool()


@pytest.fixture
def write_file():
    return WriteFileTool()


@pytest.fixture
def edit_file():
    return EditFileTool()


class TestReadFile:
    def test_name(self, read_file):
        assert read_file.name() == "read_file"

    @pytest.mark.asyncio
    async def test_read_ok(self, read_file, ctx, tmp_root):
        f = tmp_root / "hello.txt"
        f.write_text("Hello World", encoding="utf-8")
        result = await read_file.execute(ctx, {"file_path": str(f)})
        assert result.success is True
        assert result.data == "Hello World"

    @pytest.mark.asyncio
    async def test_read_not_found(self, read_file, ctx):
        result = await read_file.execute(ctx, {"file_path": "/nonexistent/path"})
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_directory(self, read_file, ctx, tmp_root):
        result = await read_file.execute(ctx, {"file_path": str(tmp_root)})
        assert result.success is False
        assert "directory" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_with_offset_limit(self, read_file, ctx, tmp_root):
        f = tmp_root / "lines.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        result = await read_file.execute(ctx, {"file_path": str(f), "offset": 1, "limit": 2})
        assert result.success is True
        assert result.data == "line2\nline3\n"
        assert result.meta["truncated"] is True
        assert result.meta["total_lines"] == 5

    @pytest.mark.asyncio
    async def test_read_meta(self, read_file, ctx, tmp_root):
        f = tmp_root / "meta.txt"
        f.write_text("test", encoding="utf-8")
        result = await read_file.execute(ctx, {"file_path": str(f)})
        assert result.meta["file_path"] == str(f)
        assert result.meta["truncated"] is False

    def test_properties(self, read_file):
        assert read_file.is_read_only() is True
        assert read_file.is_destructive() is False
        assert read_file.is_concurrency_safe({}) is True
        assert read_file.category() == "file"

    @pytest.mark.asyncio
    async def test_default_limit_2000(self, read_file, ctx, tmp_root):
        """未显式传 limit 时默认读 2000 行,超大文件被截断(对齐参考项目)。"""
        f = tmp_root / "big.txt"
        f.write_text("".join(f"line{i}\n" for i in range(2500)), encoding="utf-8")
        result = await read_file.execute(ctx, {"file_path": str(f)})
        assert result.success is True
        # 默认 limit=2000 → 到 2000 行截断
        assert result.data == "".join(f"line{i}\n" for i in range(2000))
        assert result.meta["truncated"] is True
        assert result.meta["total_lines"] == 2500


class TestWriteFile:
    def test_name(self, write_file):
        assert write_file.name() == "write_file"

    @pytest.mark.asyncio
    async def test_write_new_file(self, write_file, ctx, tmp_root):
        f = tmp_root / "new.txt"
        result = await write_file.execute(ctx, {"file_path": str(f), "content": "Hello"})
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "Hello"

    @pytest.mark.asyncio
    async def test_overwrite(self, write_file, ctx, tmp_root):
        f = tmp_root / "overwrite.txt"
        f.write_text("Old", encoding="utf-8")
        result = await write_file.execute(ctx, {"file_path": str(f), "content": "New"})
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "New"

    @pytest.mark.asyncio
    async def test_append(self, write_file, ctx, tmp_root):
        f = tmp_root / "append.txt"
        f.write_text("Base ", encoding="utf-8")
        result = await write_file.execute(ctx, {"file_path": str(f), "content": "Appended", "append": True})
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "Base Appended"

    @pytest.mark.asyncio
    async def test_auto_create_parent_dir(self, write_file, ctx, tmp_root):
        f = tmp_root / "sub" / "deep" / "file.txt"
        result = await write_file.execute(ctx, {"file_path": str(f), "content": "Auto dir"})
        assert result.success is True
        assert f.exists()

    def test_properties(self, write_file):
        assert write_file.is_read_only() is False
        assert write_file.is_destructive() is True
        assert write_file.is_concurrency_safe({}) is False
        assert write_file.category() == "file"


class TestEditFile:
    def test_name(self, edit_file):
        assert edit_file.name() == "edit_file"

    @pytest.mark.asyncio
    async def test_single_edit(self, edit_file, ctx, tmp_root):
        f = tmp_root / "single.txt"
        f.write_text("hello world", encoding="utf-8")
        result = await edit_file.execute(
            ctx,
            {"file_path": str(f), "edits": [{"old_string": "world", "new_string": "there"}]},
        )
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "hello there"
        assert result.meta["edits_applied"] == 1

    @pytest.mark.asyncio
    async def test_multi_edit(self, edit_file, ctx, tmp_root):
        f = tmp_root / "multi.txt"
        f.write_text("A\nB\nC\n", encoding="utf-8")
        result = await edit_file.execute(
            ctx,
            {
                "file_path": str(f),
                "edits": [
                    {"old_string": "A", "new_string": "X"},
                    {"old_string": "B", "new_string": "Y"},
                    {"old_string": "C", "new_string": "Z"},
                ],
            },
        )
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "X\nY\nZ\n"
        assert result.meta["edits_applied"] == 3

    @pytest.mark.asyncio
    async def test_rollback_on_failure(self, edit_file, ctx, tmp_root):
        """When the 2nd edit fails, the file must remain unchanged."""
        f = tmp_root / "rollback.txt"
        original = "First line\nSecond line\nThird line\n"
        f.write_text(original, encoding="utf-8")
        result = await edit_file.execute(
            ctx,
            {
                "file_path": str(f),
                "edits": [
                    {"old_string": "First", "new_string": "1st"},
                    {"old_string": "NOT_FOUND", "new_string": "GHOST"},
                    {"old_string": "Third", "new_string": "3rd"},
                ],
            },
        )
        assert result.success is False
        assert "failed" in result.error.lower()
        # File must be unchanged
        assert f.read_text(encoding="utf-8") == original
        assert result.meta["edits_applied"] == 1

    @pytest.mark.asyncio
    async def test_rollback_first_edit_fails(self, edit_file, ctx, tmp_root):
        f = tmp_root / "rollback_first.txt"
        original = "only line"
        f.write_text(original, encoding="utf-8")
        result = await edit_file.execute(
            ctx,
            {
                "file_path": str(f),
                "edits": [{"old_string": "NOT_THERE", "new_string": "X"}],
            },
        )
        assert result.success is False
        assert f.read_text(encoding="utf-8") == original

    @pytest.mark.asyncio
    async def test_dry_run(self, edit_file, ctx, tmp_root):
        f = tmp_root / "dryrun.txt"
        original = "Hello World"
        f.write_text(original, encoding="utf-8")
        result = await edit_file.execute(
            ctx,
            {
                "file_path": str(f),
                "edits": [{"old_string": "Hello", "new_string": "Hi"}],
                "dry_run": True,
            },
        )
        assert result.success is True
        assert result.meta["dry_run"] is True
        assert result.meta["edits_matched"] == 1
        # File must NOT be changed
        assert f.read_text(encoding="utf-8") == original

    @pytest.mark.asyncio
    async def test_dry_run_not_found(self, edit_file, ctx, tmp_root):
        """Dry run with a non-matching edit should still succeed (just report)."""
        f = tmp_root / "dryrun_miss.txt"
        f.write_text("Hello", encoding="utf-8")
        result = await edit_file.execute(
            ctx,
            {
                "file_path": str(f),
                "edits": [{"old_string": "NOT_FOUND", "new_string": "X"}],
                "dry_run": True,
            },
        )
        assert result.success is True
        assert result.meta["edits_matched"] == 0

    @pytest.mark.asyncio
    async def test_old_string_must_be_unique(self, edit_file, ctx, tmp_root):
        """old_string 出现多次 → 判为歧义,报错且回滚(对齐参考项目 edit_file)。"""
        f = tmp_root / "unique.txt"
        f.write_text("A A A", encoding="utf-8")
        result = await edit_file.execute(
            ctx,
            {"file_path": str(f), "edits": [{"old_string": "A", "new_string": "X"}]},
        )
        assert result.success is False
        assert "must be unique" in result.error
        assert f.read_text(encoding="utf-8") == "A A A"  # 回滚,文件未变

    @pytest.mark.asyncio
    async def test_file_not_found(self, edit_file, ctx):
        result = await edit_file.execute(
            ctx,
            {"file_path": "/nonexistent_file_xyz", "edits": [{"old_string": "a", "new_string": "b"}]},
        )
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_properties(self, edit_file):
        assert edit_file.is_read_only() is False
        assert edit_file.is_destructive() is True
        assert edit_file.is_concurrency_safe({}) is False
        assert edit_file.category() == "file"
