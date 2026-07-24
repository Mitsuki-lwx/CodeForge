from __future__ import annotations

from pathlib import Path

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class WriteFileTool(Tool):
    """Write content to a file. Automatically creates parent directories."""

    timeout_seconds = 30.0

    def name(self) -> str:
        return "write_file"

    def description(self) -> str:
        return (
            "Write content to a file. Creates parent directories if needed. Supports append mode. "
            "For modifying existing files, prefer edit_file and read the file first."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean"},
            },
            "required": ["file_path", "content"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        file_path = Path(input["file_path"])
        if not file_path.is_absolute():
            file_path = context.cwd / file_path

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)

            if input.get("append", False):
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(input["content"])
            else:
                file_path.write_text(input["content"], encoding="utf-8")
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                meta={"file_path": str(file_path)},
            )

        return ToolResult(
            success=True,
            data=None,
            meta={
                "file_path": str(file_path),
                "append": input.get("append", False),
            },
        )

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def category(self) -> str:
        return "file"
