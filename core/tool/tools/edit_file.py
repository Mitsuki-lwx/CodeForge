from __future__ import annotations

from pathlib import Path

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class EditFileTool(Tool):
    """Apply a sequence of string replacements to a file.

    Edits are applied in order. If *any* edit fails to find its *old_string*,
    **all** edits are rolled back and the file is left unchanged.
    """

    timeout_seconds = 30.0

    def name(self) -> str:
        return "edit_file"

    def description(self) -> str:
        return (
            "Apply multiple string replacements to a file. "
            "Edits are sequenced; if any edit fails all are rolled back. "
            "You MUST read the file first before editing it."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                    "minItems": 1,
                },
                "dry_run": {"type": "boolean"},
            },
            "required": ["file_path", "edits"],
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
            original = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Cannot read file: {e}",
                meta={"file_path": str(file_path)},
            )

        edits = input["edits"]
        dry_run = input.get("dry_run", False)
        current = original
        applied = 0
        match_results: list[dict] = []

        for i, edit in enumerate(edits):
            old = edit["old_string"]
            new = edit["new_string"]

            if old not in current:
                match_results.append(
                    {"index": i, "matched": False, "old_string": old}
                )
                if not dry_run:
                    # Rollback: write original back
                    try:
                        file_path.write_text(original, encoding="utf-8")
                    except Exception as e:
                        return ToolResult(
                            success=False,
                            error=f"Rollback failed: {e}",
                            meta={"file_path": str(file_path)},
                        )
                    return ToolResult(
                        success=False,
                        error=f"Edit #{i} failed: old_string not found in file",
                        meta={
                            "file_path": str(file_path),
                            "edits_applied": applied,
                            "edits_total": len(edits),
                            "failed_index": i,
                        },
                    )
                else:
                    # In dry run, just report and continue
                    continue

            # Replace only the FIRST occurrence
            current = current.replace(old, new, 1)
            applied += 1
            match_results.append({"index": i, "matched": True, "old_string": old})

        if dry_run:
            return ToolResult(
                success=True,
                data=None,
                meta={
                    "file_path": str(file_path),
                    "dry_run": True,
                    "edits_total": len(edits),
                    "edits_matched": applied,
                    "match_results": match_results,
                },
            )

        # Write final result
        try:
            file_path.write_text(current, encoding="utf-8")
        except Exception as e:
            # Write failed — rollback
            try:
                file_path.write_text(original, encoding="utf-8")
            except Exception:
                pass
            return ToolResult(
                success=False,
                error=f"Write failed after all edits matched: {e}",
                meta={"file_path": str(file_path), "edits_applied": applied},
            )

        return ToolResult(
            success=True,
            data=None,
            meta={
                "file_path": str(file_path),
                "edits_applied": applied,
                "edits_total": len(edits),
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
