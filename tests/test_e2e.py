"""End-to-end integration tests for the Tool system."""

import tempfile
from pathlib import Path

import pytest

from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry


@pytest.fixture
def tmp_root():
    with tempfile.TemporaryDirectory(prefix="codeforge_e2e_") as d:
        yield Path(d)


@pytest.mark.asyncio
async def test_e2e_normal_workflow(tmp_root):
    """Scenario A: write → edit → read."""
    reg = get_default_registry()
    ctx = ExecutionContext(cwd=tmp_root, session_id="e2e-a")
    f = tmp_root / "a.txt"

    # Write
    r1 = await reg.execute("write_file", ctx, {"file_path": str(f), "content": "Hello"})
    assert r1.success is True

    # Edit
    r2 = await reg.execute(
        "edit_file",
        ctx,
        {"file_path": str(f), "edits": [{"old_string": "Hello", "new_string": "World"}]},
    )
    assert r2.success is True

    # Read
    r3 = await reg.execute("read_file", ctx, {"file_path": str(f)})
    assert r3.success is True
    assert r3.data == "World"


@pytest.mark.asyncio
async def test_e2e_rollback(tmp_root):
    """Scenario B: edit fails → file is unchanged."""
    reg = get_default_registry()
    ctx = ExecutionContext(cwd=tmp_root, session_id="e2e-b")
    f = tmp_root / "b.txt"

    await reg.execute("write_file", ctx, {"file_path": str(f), "content": "ABC"})

    # Edit with a failing middle segment
    r = await reg.execute(
        "edit_file",
        ctx,
        {
            "file_path": str(f),
            "edits": [
                {"old_string": "A", "new_string": "X"},
                {"old_string": "NOT_FOUND", "new_string": "Y"},
                {"old_string": "C", "new_string": "Z"},
            ],
        },
    )
    assert r.success is False

    # Verify unchanged
    r2 = await reg.execute("read_file", ctx, {"file_path": str(f)})
    assert r2.data == "ABC"


@pytest.mark.asyncio
async def test_e2e_bash_and_grep(tmp_root):
    """Scenario C: bash creates file → grep searches it."""
    reg = get_default_registry()
    ctx = ExecutionContext(cwd=tmp_root, session_id="e2e-c")
    f = tmp_root / "test_grep.txt"

    # Bash: create file
    cmd = f"echo -e 'foo\\nbar\\nbaz' > {f}"
    r = await reg.execute("bash", ctx, {"command": cmd})
    assert r.success is True, f"bash failed: {r.error}"

    # Grep for "bar"
    r2 = await reg.execute("grep", ctx, {"pattern": "bar", "path": str(tmp_root)})
    assert r2.success is True
    assert len(r2.data) >= 1
    assert "bar" in r2.data[0]["text"]


@pytest.mark.asyncio
async def test_e2e_get_default_registry_has_seven_tools():
    reg = get_default_registry()
    tools = reg.list()
    names = {t.name() for t in tools}
    assert names == {"read_file", "write_file", "edit_file", "bash", "glob", "grep", "ExitPlanMode"}
