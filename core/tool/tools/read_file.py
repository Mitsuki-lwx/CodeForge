from __future__ import annotations

from pathlib import Path

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class ReadFileTool(Tool):
    """Read the contents of a file, optionally with line offset/limit."""

    timeout_seconds = 30.0

    def name(self) -> str:
        return "read_file"

    def description(self) -> str:
        return "Read the contents of a file. Supports optional line offset and limit for partial reads."

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["file_path"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        file_path = Path(input["file_path"])
        if not file_path.is_absolute():
            file_path = context.cwd / file_path

        if not file_path.exists():
            return ToolResult(
                success=False,
                error=f"File not found: {file_path}",
                meta={"file_path": str(file_path)},
            )
        if file_path.is_dir():
            return ToolResult(
                success=False,
                error=f"Path is a directory: {file_path}",
                meta={"file_path": str(file_path)},
            )

        try:
            text = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                meta={"file_path": str(file_path)},
            )

        lines = text.splitlines(keepends=True)
        offset = input.get("offset", 0)
        limit = input.get("limit", None)

        if offset > 0 or limit is not None:
            if offset >= len(lines):
                return ToolResult(
                    success=False,
                    error=f"Offset {offset} exceeds file length ({len(lines)} lines)",
                    meta={"file_path": str(file_path), "total_lines": len(lines)},
                )
            end = offset + limit if limit else len(lines)
            sliced = lines[offset:end]
            truncated = end < len(lines)
        else:
            sliced = lines
            truncated = False

        content = "".join(sliced)
        return ToolResult(
            success=True,
            data=content,
            meta={
                "file_path": str(file_path),
                "total_lines": len(lines),
                "truncated": truncated,
                "offset": offset,
                "limit": limit,
            },
        )

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "file"
