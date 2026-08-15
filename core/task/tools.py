"""后台任务管理工具 —— TaskList / TaskGet / TaskStop / SendMessage。

注册到 ToolRegistry，让主 Agent 主动查询和操控后台子 Agent。
"""

from __future__ import annotations

import json
from typing import Any

from core.task.manager import (
    BackgroundTask,
    BackgroundTaskManager,
    TaskBusyError,
    TaskNotFoundError,
    TaskStatus,
)
from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult

# ── TaskList ───────────────────────────────────────────────────────


class TaskListTool(Tool):
    """列出当前所有非 Terminated 后台任务的简要信息。"""

    def __init__(self, manager: BackgroundTaskManager) -> None:
        self._manager = manager
        self.is_system_tool = True

    def name(self) -> str:
        return "TaskList"

    def description(self) -> str:
        return "List all running background sub-agent tasks with id, name, status, and tool count."

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "read"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        tasks = self._manager.list()
        items = []
        for bt in tasks:
            items.append({
                "id": bt.id,
                "name": bt.name,
                "status": _status_str(bt.status),
                "tool_count": bt.tool_count,
                "last_activity": bt.last_activity,
            })
        return ToolResult(
            success=True,
            data=json.dumps(items, ensure_ascii=False, indent=2),
        )


# ── TaskGet ────────────────────────────────────────────────────────


class TaskGetTool(Tool):
    """获取指定后台任务的完整状态。"""

    def __init__(self, manager: BackgroundTaskManager) -> None:
        self._manager = manager
        self.is_system_tool = True

    def name(self) -> str:
        return "TaskGet"

    def description(self) -> str:
        return "Get the full status of a specific background sub-agent task, including result if completed."

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ID of the background task to query.",
                },
            },
            "required": ["task_id"],
        }

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "read"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        task_id = input.get("task_id", "")
        bt = self._manager.get(task_id)
        if bt is None:
            return ToolResult(
                success=False,
                error=f"Task '{task_id}' not found",
            )

        return ToolResult(
            success=True,
            data=json.dumps(_task_to_dict(bt), ensure_ascii=False, indent=2),
        )


# ── TaskStop ───────────────────────────────────────────────────────


class TaskStopTool(Tool):
    """停止一个运行中的后台任务。"""

    def __init__(self, manager: BackgroundTaskManager) -> None:
        self._manager = manager
        self.is_system_tool = True

    def name(self) -> str:
        return "TaskStop"

    def description(self) -> str:
        return "Cancel a running background sub-agent task."

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ID of the background task to cancel.",
                },
            },
            "required": ["task_id"],
        }

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        task_id = input.get("task_id", "")
        ok = await self._manager.stop(task_id)
        if not ok:
            return ToolResult(
                success=False,
                error=f"Task '{task_id}' not found",
            )
        return ToolResult(
            success=True,
            data=json.dumps({"task_id": task_id, "status": "cancellation_requested"}),
        )


# ── SendMessage ────────────────────────────────────────────────────


class SendMessageTool(Tool):
    """向已完成的存活后台 Agent 续派新任务。"""

    def __init__(self, manager: BackgroundTaskManager) -> None:
        self._manager = manager
        self.is_system_tool = True

    def name(self) -> str:
        return "SendMessage"

    def description(self) -> str:
        return "Send a follow-up task to a completed background sub-agent by its name."

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the background sub-agent (as given in the Agent tool call).",
                },
                "message": {
                    "type": "string",
                    "description": "The new task message to send.",
                },
            },
            "required": ["name", "message"],
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
        name = input.get("name", "")
        message = input.get("message", "")
        try:
            task_id = await self._manager.send_message(name, message)
        except TaskNotFoundError as e:
            return ToolResult(success=False, error=str(e))
        except TaskBusyError as e:
            return ToolResult(success=False, error=str(e))

        return ToolResult(
            success=True,
            data=json.dumps({"task_id": task_id, "status": "resumed"}),
        )


# ── 辅助 ───────────────────────────────────────────────────────────


def _status_str(status: TaskStatus) -> str:
    return {TaskStatus.RUNNING: "running", TaskStatus.COMPLETED: "completed",
            TaskStatus.FAILED: "failed", TaskStatus.CANCELLED: "cancelled"}[status]


def _task_to_dict(bt: BackgroundTask) -> dict[str, Any]:
    return {
        "id": bt.id,
        "name": bt.name,
        "status": _status_str(bt.status),
        "result": bt.result,
        "task": bt.task,
        "tool_count": bt.tool_count,
        "last_activity": bt.last_activity,
        "usage": {
            "input": bt.usage.input,
            "output": bt.usage.output,
        },
    }
