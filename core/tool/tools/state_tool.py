"""会话状态工具 —— 供模型主动记录 目标 / 待办 / 硬性约束。

spec_session_state：三层写入之一（agent 工具）。工具构造时注入 `SessionStateStore`
实例（由 tui/app 在启动时创建，绑定当前会话目录 + NoteStore）。子 agent 的 registry
不含这些工具（见 _apply_filter，保证只读继承）。
"""

from __future__ import annotations

from typing import Any

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class SetGoalTool(Tool):
    """记录/更新当前会话的用户目标。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    def name(self) -> str:
        return "SetGoal"

    def description(self) -> str:
        return (
            "Record or update the current session goal (what the user wants to "
            "accomplish now). Overwrites the previous goal."
        )

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"goal": {"type": "string", "description": "The session goal."}},
            "required": ["goal"],
        }

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        text = (input.get("goal") or "").strip()
        if not text:
            return ToolResult(success=False, error="goal is required")
        self._store.set_goal(text)
        return ToolResult(success=True, data="已记录目标")


class AddTodoTool(Tool):
    """新增一条会话待办。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    def name(self) -> str:
        return "AddTodo"

    def description(self) -> str:
        return "Add a todo item to the current session's task list."

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Todo item text."}},
            "required": ["text"],
        }

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        text = (input.get("text") or "").strip()
        if not text:
            return ToolResult(success=False, error="text is required")
        self._store.add_todo(text)
        return ToolResult(success=True, data="已添加待办")


class AddConstraintTool(Tool):
    """记录一条硬性约束（默认会话级；由用户 /constraint promote 显式提升持久）。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    def name(self) -> str:
        return "AddConstraint"

    def description(self) -> str:
        return (
            "Record a hard constraint the user must respect (e.g. do not modify a "
            "specific file). Session-scoped by default."
        )

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "constraint": {"type": "string", "description": "The constraint text."}
            },
            "required": ["constraint"],
        }

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        text = (input.get("constraint") or "").strip()
        if not text:
            return ToolResult(success=False, error="constraint is required")
        self._store.add_constraint(text)
        return ToolResult(success=True, data="已记录硬性约束（会话级）")


def register_state_tools(registry, store) -> None:
    """注册三个状态工具到 registry。"""

    registry.register(SetGoalTool(store))
    registry.register(AddTodoTool(store))
    registry.register(AddConstraintTool(store))


__all__ = [
    "AddConstraintTool",
    "AddTodoTool",
    "SetGoalTool",
    "register_state_tools",
]
