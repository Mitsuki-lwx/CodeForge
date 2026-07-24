from __future__ import annotations

import asyncio
import os
import sys

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class BashTool(Tool):
    """Execute a shell command asynchronously and return its output."""

    timeout_seconds = 120.0

    def name(self) -> str:
        return "bash"

    def description(self) -> str:
        return (
            "Execute a shell command. Returns stdout, stderr, and exit code. "
            "Prefer dedicated tools (read_file, write_file, edit_file, glob, grep) "
            "over shell commands whenever a dedicated tool exists for the task."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "description": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1},
            },
            "required": ["command"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        command = input["command"]
        timeout = input.get("timeout", self.timeout_seconds)

        try:
            if sys.platform == "win32":
                # On Windows, use CREATE_NO_WINDOW to avoid flashing cmd windows
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(context.cwd),
                    env={**os.environ, **context.env} if context.env else None,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(context.cwd),
                    env={**os.environ, **context.env} if context.env else None,
                )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return ToolResult(
                    success=False,
                    error=f"Command timed out after {timeout}s",
                    meta={
                        "command": command,
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": f"<timed out after {timeout}s>",
                    },
                )

            stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""
            exit_code = proc.returncode or 0

            return ToolResult(
                success=exit_code == 0,
                data=stdout_str,
                error=stderr_str if exit_code != 0 else None,
                meta={
                    "command": command,
                    "exit_code": exit_code,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e),
                meta={"command": command, "exit_code": -1},
            )

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def category(self) -> str:
        return "shell"
