from __future__ import annotations

from pathlib import Path

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult
from core.tool.tools import SKIP_DIRS, _path_climbs_to_skip


class GlobTool(Tool):
    """List files matching a glob pattern."""

    timeout_seconds = 30.0

    def name(self) -> str:
        return "glob"

    def description(self) -> str:
        return "List files matching a glob pattern. Returns relative paths."

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        pattern = input["pattern"]
        root = Path(input["path"]) if input.get("path") else context.cwd
        if not root.is_absolute():
            root = context.cwd / root

        if not root.exists():
            return ToolResult(
                success=False,
                error=f"Directory not found: {root}",
                meta={"pattern": pattern, "path": str(root)},
            )

        try:
            matches = [
                str(p.relative_to(root))
                for p in root.rglob(pattern)
                if p.is_file() and not _path_climbs_to_skip(p)
            ]
            matches.sort()
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                meta={"pattern": pattern, "path": str(root)},
            )

        return ToolResult(
            success=True,
            data=matches,
            meta={"pattern": pattern, "path": str(root), "count": len(matches)},
        )

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "code_search"
