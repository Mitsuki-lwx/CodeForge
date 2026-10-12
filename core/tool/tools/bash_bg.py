"""后台 shell 的读写工具 —— `bash_output` 与 `kill_shell`。

配套 `BashTool(run_in_background=true)`（见 `core/tool/shells.py` 的模块说明）。
分工刻意分开：

* `bash_output` 是**只读**工具（`category="read"`）—— 读自己刚跑起来的进程的
  输出没有副作用，不该每次都弹审批，否则"起服务→看日志→访问"这条链路会被
  审批打断到没法用。
* `kill_shell` 是**破坏性**工具（`category="shell"` → 权限档 `command`）——
  它会终止用户的进程，必须过权限层。

两者都 `max_retries = 0`：重试没有意义（读两次输出、杀两次进程），
而且重试会把"没找到这个 shell_id"变成三次无谓的查找。
"""

from __future__ import annotations

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult
from core.tool.shells import MAX_WAIT_MS, describe

#: `bash_output` 单次返回的字符上限（防止把整个 200KB 缓冲灌进上下文）
MAX_RETURN_CHARS = 20_000


class BashOutputTool(Tool):
    """读一个后台 shell 的累积输出（可短暂等待它结束）。"""

    timeout_seconds = 40.0
    max_retries = 0

    def name(self) -> str:
        return "bash_output"

    def description(self) -> str:
        return (
            "Read the accumulated output of a background shell started with "
            "bash(run_in_background=true). Returns status (running/exited), "
            "exit code, and output tail. Pass wait_ms to block briefly for it to "
            "finish (useful right after starting a server or a build)."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "shell_id": {"type": "string", "description": "bash 返回的 shell_id"},
                "wait_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_WAIT_MS,
                    "description": f"最多等这么久（毫秒，上限 {MAX_WAIT_MS}）",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 100,
                    "description": "只取输出尾部这么多字符",
                },
            },
            "required": ["shell_id"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        shell_id = str(input["shell_id"])
        rec = context.shells.get(shell_id)
        if rec is None:
            known = ", ".join(r.id for r in context.shells.list()) or "（无）"
            return ToolResult(
                success=False,
                error=f"没有这个后台 shell: {shell_id}。当前有: {known}",
                meta={"shell_id": shell_id},
            )

        wait_ms = int(input.get("wait_ms") or 0)
        await context.shells.wait(rec, wait_ms)

        limit = int(input.get("max_chars") or MAX_RETURN_CHARS)
        text = rec.output()
        cut = ""
        if len(text) > limit:
            cut = f"\n…（前面 {len(text) - limit} 字符已省略）"
            text = text[-limit:]

        header = f"{describe(rec)}"
        if rec.truncated:
            header += "（缓冲已截断，保留的是尾部）"
        body = text.strip() or "（暂无输出）"
        return ToolResult(
            success=True,
            data=f"{header}\n---\n{body}{cut}",
            meta={
                "shell_id": rec.id,
                "status": "running" if rec.running else "exited",
                "exit_code": rec.exit_code,
            },
        )

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "read"


class KillShellTool(Tool):
    """终止一个后台 shell。"""

    timeout_seconds = 15.0
    max_retries = 0

    def name(self) -> str:
        return "kill_shell"

    def description(self) -> str:
        return (
            "Stop a background shell started with bash(run_in_background=true). "
            "Kills the process. Call this when you are done with a dev server or "
            "watcher so it does not keep the port or the CPU."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "shell_id": {"type": "string", "description": "bash 返回的 shell_id"}
            },
            "required": ["shell_id"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        shell_id = str(input["shell_id"])
        rec = context.shells.get(shell_id)
        if rec is None:
            known = ", ".join(r.id for r in context.shells.list()) or "（无）"
            return ToolResult(
                success=False,
                error=f"没有这个后台 shell: {shell_id}。当前有: {known}",
                meta={"shell_id": shell_id},
            )
        was_running = rec.running
        context.shells.kill(shell_id)
        return ToolResult(
            success=True,
            data=(
                f"{shell_id} 已停止（{'杀掉了运行中的进程' if was_running else '进程本来已退出'}）"
            ),
            meta={"shell_id": shell_id, "was_running": was_running},
        )

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def category(self) -> str:
        return "shell"


__all__ = ["BashOutputTool", "KillShellTool"]
