"""Tests for Tool abstract base class and validate_input."""

import pytest
from jsonschema import ValidationError

from core.tool.context import ExecutionContext
from core.tool.errors import ToolValidationError
from core.tool.interface import Tool
from core.tool.result import ToolResult


class _ConcreteTool(Tool):
    """Minimal concrete tool for testing the abstract base class."""

    def name(self) -> str:
        return "test_tool"

    def description(self) -> str:
        return "A test tool"

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer", "minimum": 1},
            },
            "required": ["name"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        return ToolResult(success=True, data=f"Hello {input['name']}")

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "test"


class _NoSchemaTool(_ConcreteTool):
    """Tool with no input_schema (returns empty dict)."""

    def input_schema(self) -> dict:
        return {}


def test_abstract_cannot_instantiate():
    with pytest.raises(TypeError):
        Tool()  # type: ignore


def test_concrete_instantiates():
    tool = _ConcreteTool()
    assert tool.name() == "test_tool"
    assert tool.category() == "test"
    assert tool.timeout_seconds == 30.0
    assert tool.max_retries == 2


@pytest.mark.asyncio
async def test_execute_concrete_tool():
    tool = _ConcreteTool()
    ctx = ExecutionContext(cwd="/tmp")
    result = await tool.execute(ctx, {"name": "World"})
    assert result.success is True
    assert result.data == "Hello World"


class TestValidateInput:
    def test_valid_input(self):
        tool = _ConcreteTool()
        assert tool.validate_input({"name": "world"}) is None
        assert tool.validate_input({"name": "world", "count": 5}) is None

    def test_missing_required(self):
        tool = _ConcreteTool()
        err = tool.validate_input({})
        assert err is not None
        assert "name" in err

    def test_wrong_type(self):
        tool = _ConcreteTool()
        err = tool.validate_input({"name": 123})
        assert err is not None

    def test_out_of_bounds(self):
        tool = _ConcreteTool()
        err = tool.validate_input({"name": "x", "count": 0})
        assert err is not None
        assert "0" in err

    def test_no_schema(self):
        tool = _NoSchemaTool()
        assert tool.validate_input({"anything": "goes"}) is None
