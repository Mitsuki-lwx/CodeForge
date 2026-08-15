from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult

MAX_TIMEOUT = 600

# 特殊命令的退出码语义:这些命令 exit=1 不算错误,>= 阈值才为真错误。
# 例:grep 返回 1 仅表示"没有匹配行",不是执行出错。对齐参考项目 tools/bash.py。
_COMMAND_ERROR_THRESHOLDS: dict[str, int] = {
    "grep": 2,
    "egrep": 2,
    "fgrep": 2,
    "rg": 2,
    "diff": 2,
    "find": 2,
    "test": 2,
    "[": 2,
}

_EXIT_CODE_HINTS: dict[str, str] = {
    "grep": "no matches found",
    "egrep": "no matches found",
    "fgrep": "no matches found",
    "rg": "no matches found",
    "diff": "files differ",
    "find": "some directories were inaccessible",
    "test": "condition is false",
    "[": "condition is false",
}


def _extract_last_command_name(command: str) -> str | None:
    """取命令最后一个管道段的基础命令名(退出码由最后一段决定)。"""
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        tokens = last_segment.split()
    for token in tokens:
        # 跳过 VAR=value 环境变量赋值前缀
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        return token.rsplit("/", maxsplit=1)[-1]
    return None


def _exit_code_hint(command: str, exit_code: int) -> str:
    """为非零退出码生成可读提示(特殊命令附语义,普通命令只给数字)。"""
    cmd_name = _extract_last_command_name(command)
    hint = _EXIT_CODE_HINTS.get(cmd_name, "") if cmd_name else ""
    if hint:
        return f"Exit code {exit_code} ({hint})"
    return f"Exit code {exit_code}"


def _is_failing_exit(command: str, exit_code: int) -> bool:
    """判断退出码是否代表真正的错误。

    - exit 0 → 非错误
    - grep/diff/find/test 等特殊命令:exit < 阈值不算错误(如 grep exit 1=无匹配)
    - 其余命令:任何非零都是错误
    """
    if exit_code == 0:
        return False
    if exit_code < 0:
        return True  # 超时/被杀,视为错误
    cmd_name = _extract_last_command_name(command)
    if cmd_name and cmd_name in _COMMAND_ERROR_THRESHOLDS:
        return exit_code >= _COMMAND_ERROR_THRESHOLDS[cmd_name]
    return True


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

            if _is_failing_exit(command, exit_code):
                # 普通命令真正失败(或特殊命令达到错误阈值)→ 判为错误
                return ToolResult(
                    success=False,
                    data=stdout_str or "(no output)",
                    error=(stderr_str or stdout_str).strip()
                    or f"Exit code {exit_code}",
                    meta={
                        "command": command,
                        "exit_code": exit_code,
                        "stdout": stdout_str,
                        "stderr": stderr_str,
                    },
                )

            # 特殊命令的"非错误"非零退出(如 grep exit 1=无匹配)或 exit 0:
            # 不判为错误,仅在输出里附退出码提示,让 agent 读到语义而不误判。
            merged = (stdout_str + ("\n" + stderr_str if stderr_str.strip() else "")).strip()
            if exit_code != 0:
                hint = _exit_code_hint(command, exit_code)
                out = f"{merged}\n(hint: {hint})" if merged else f"(no output)\n(hint: {hint})"
                data = out
            else:
                data = merged or "(no output)"
            return ToolResult(
                success=True,
                data=data,
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
