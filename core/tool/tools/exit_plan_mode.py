"""ExitPlanMode 工具 —— 模型主动退出 Plan Mode。

对齐 Mewcode tools/exit_plan_mode.py。
"""

from __future__ import annotations

from typing import Callable

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class ExitPlanModeTool(Tool):
    """模型在写完 plan 后调用此工具提交审批。"""

    timeout_seconds = 5.0

    def __init__(
        self,
        is_plan_mode: Callable[[], bool] | None = None,
        plan_exists: Callable[[], bool] | None = None,
    ) -> None:
        self._is_plan_mode = is_plan_mode or (lambda: False)
        self._plan_exists = plan_exists or (lambda: False)

    def name(self) -> str:
        return "ExitPlanMode"

    def description(self) -> str:
        return (
            "Exit plan mode and present the plan for user approval. "
            "Call this when your plan is complete and written to the plan file. "
            "After calling this, do NOT make any more tool calls — end your turn."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        if not self._is_plan_mode():
            return ToolResult(
                success=False,
                error="You are not in plan mode. This tool is only for exiting plan mode after writing a plan.",
            )
        if not self._plan_exists():
            return ToolResult(
                success=False,
                error="No plan file found. Please write your plan to the plan file before calling ExitPlanMode.",
            )
        return ToolResult(
            success=True,
            data=(
                "Plan mode will be exited after this turn. "
                "The user will be shown the plan approval dialog. "
                "Do not call any more tools — end your turn now."
            ),
        )

    def is_read_only(self) -> bool:
        return True  # Plan mode 下始终放行

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "plan"
