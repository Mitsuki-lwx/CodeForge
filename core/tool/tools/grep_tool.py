from __future__ import annotations

import re
from pathlib import Path

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult
from core.tool.tools import SKIP_DIRS, _path_climbs_to_skip


class GrepTool(Tool):
    """Search file contents for a regex pattern (pure Python implementation)."""

    timeout_seconds = 30.0

    def name(self) -> str:
        return "grep"

    def description(self) -> str:
        return (
            "Search file contents for a regex pattern. "
            "Supports file-type filtering and multiple output modes."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "include": {"type": "string"},
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        pattern_str = input["pattern"]
        root = Path(input["path"]) if input.get("path") else context.cwd
        if not root.is_absolute():
            root = context.cwd / root

        if not root.exists():
            return ToolResult(
                success=False,
                error=f"Path not found: {root}",
                meta={"pattern": pattern_str, "path": str(root)},
            )

        include_glob = input.get("include")
        output_mode = input.get("output_mode", "content")

        try:
            regex = re.compile(pattern_str)
        except re.error as e:
            return ToolResult(
                success=False,
                error=f"Invalid regex: {e}",
                meta={"pattern": pattern_str},
            )

        matches: list[dict] = []
        file_set: set[str] = set()
        total_count = 0

        try:
            files = list(root.rglob(include_glob)) if include_glob else list(root.rglob("*"))
            for f in files:
                if not f.is_file():
                    continue
                # 跳过 .venv/.git/node_modules/__pycache__ 等重型目录(对齐参考项目)
                if _path_climbs_to_skip(f):
                    continue
                try:
                    for line_no, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                        if regex.search(line):
                            rel = str(f.relative_to(root))
                            matches.append({"file": rel, "line": line_no, "text": line})
                            file_set.add(rel)
                            total_count += 1
                except (OSError, UnicodeDecodeError):
                    # Skip binary/unreadable files
                    continue
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                meta={"pattern": pattern_str, "path": str(root)},
            )

        if output_mode == "count":
            return ToolResult(
                success=True,
                data=total_count,
                meta={
                    "pattern": pattern_str,
                    "path": str(root),
                    "count": total_count,
                    "files": len(file_set),
                },
            )
        elif output_mode == "files_with_matches":
            return ToolResult(
                success=True,
                data=sorted(file_set),
                meta={
                    "pattern": pattern_str,
                    "path": str(root),
                    "count": len(file_set),
                },
            )
        else:  # content
            return ToolResult(
                success=True,
                data=matches,
                meta={
                    "pattern": pattern_str,
                    "path": str(root),
                    "count": len(matches),
                    "files": len(file_set),
                },
            )

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "code_search"
