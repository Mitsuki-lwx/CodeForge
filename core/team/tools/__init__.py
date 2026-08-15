"""团队协作工具 —— TaskCreate/TaskGet/TaskList/TaskUpdate/SendMessage + build_teammate_tools。

仿照参考实现 `mewcode.agents.tool_filter.build_teammate_tools`：
这些工具是**每次 spawn 新建的 tool 实例**，构造时绑定 `(team_manager, team_name[, agent_name/agent_id])`，
在 `build_teammate_tools` 中生成一份**专用 teammate registry**，供队友 Agent 使用。
因此队友的 `SendMessage`/`TaskList` 恒为 team-bound 版本，与全局 registry 里的后台任务版
（core/task/tools.py）不冲突。
"""

from __future__ import annotations

import logging
from typing import Any

from core.tool.interface import Tool
from core.tool.result import ToolResult

logger = logging.getLogger(__name__)


class _TeamBoundMixin:
    """让协作工具容忍 team_name 为空：无绑定时用 active_team 解析。"""

    # 子类提供 _team_manager / _team_name
    def _resolve_team_name(self) -> str:
        name = getattr(self, "_team_name", "") or ""
        if name:
            return name
        mgr = getattr(self, "_team_manager", None)
        if mgr is not None and hasattr(mgr, "active_team"):
            team = mgr.active_team()
            if team is not None:
                return team.sanitized_name
        return ""

    def _team_ok(self) -> bool:
        return bool(self._resolve_team_name())

# 团队全栈注册表里被隐藏的元工具。
_TEAM_LEAD_ONLY_TOOLS = {"TeamCreate", "TeamDelete"}

# in-process 队友的工具白名单 ≈ 后台白名单 ∪ 协作工具。
IN_PROCESS_TEAMMATE_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        # 读写类（复用后台白名单，见 core/tool/filter.py）
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "bash",
        "load_skill",
        "install_skill",
        # 协作工具
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskUpdate",
        "SendMessage",
    }
)


# ── 共享任务工具 ──────────────────────────────────────────────────


class TaskCreateTool(_TeamBoundMixin, Tool):
    """在共享任务板上新建一条任务，返回 task_id。"""

    is_system_tool = True

    def __init__(self, team_manager: Any, team_name: str, agent_name: str = "") -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._agent_name = agent_name

    def name(self) -> str:
        return "TaskCreate"

    def description(self) -> str:
        return "Create a shared task on this team's task board. Returns the new task id."

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title (required)."},
                "description": {"type": "string", "description": "Optional detail."},
                "assignee": {"type": "string", "description": "Optional assignee name."},
                "blocked_by": {"type": "array", "items": {"type": "string"},
                               "description": "Optional ids this task depends on."},
            },
            "required": ["title"],
        }

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def category(self) -> str:
        return "task"

    async def execute(self, context: Any, input: dict) -> ToolResult:
        from core.team.tasks import Task

        store = self._team_manager.get_task_store(self._resolve_team_name())
        if store is None:
            return ToolResult(success=False, error=f"未找到团队 '{self._team_name}'")
        task = Task(
            id="",
            title=input.get("title", ""),
            description=input.get("description", ""),
            assignee=input.get("assignee", ""),
            blocked_by=list(input.get("blocked_by", [])),
        )
        task_id = await store.create(task)
        return ToolResult(success=True, data=task_id)


class TaskGetTool(_TeamBoundMixin, Tool):
    """按 task_id 取一条共享任务详情。"""

    is_system_tool = True

    def __init__(self, team_manager: Any, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    def name(self) -> str:
        return "TaskGet"

    def description(self) -> str:
        return "Get one shared task by its id from this team's task board."

    def input_schema(self) -> dict:
        return {"type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"]}

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "read"

    async def execute(self, context: Any, input: dict) -> ToolResult:
        store = self._team_manager.get_task_store(self._resolve_team_name())
        if store is None:
            return ToolResult(success=False, error=f"未找到团队 '{self._team_name}'")
        task = await store.get(input.get("task_id", ""))
        if task is None:
            return ToolResult(success=False, error="任务不存在")
        return ToolResult(success=True, data=task.to_dict())


class TaskListTool(_TeamBoundMixin, Tool):
    """列出团队共享任务板上的任务，可选按 status/assignee 过滤。"""

    is_system_tool = True

    def __init__(self, team_manager: Any, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    def name(self) -> str:
        return "TaskList"

    def description(self) -> str:
        return "List shared tasks on this team's task board, optionally filtered by status or assignee."

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "pending/in_progress/completed/blocked"},
                "assignee": {"type": "string"},
            },
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

    async def execute(self, context: Any, input: dict) -> ToolResult:
        from core.team.tasks import Filter

        store = self._team_manager.get_task_store(self._resolve_team_name())
        if store is None:
            return ToolResult(success=False, error=f"未找到团队 '{self._team_name}'")
        status_str = input.get("status")
        f = Filter(status=_parse_status(status_str)) if status_str else None
        tasks = await store.list_(f)
        data = [t.to_dict() for t in tasks]
        return ToolResult(success=True, data=data)


class TaskUpdateTool(_TeamBoundMixin, Tool):
    """更新一条共享任务（含双向依赖维护），返回更新后的任务。"""

    is_system_tool = True

    def __init__(self, team_manager: Any, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    def name(self) -> str:
        return "TaskUpdate"

    def description(self) -> str:
        return "Update a shared task's fields and dependency edges (blocks/blocked_by)."

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string", "description": "pending/in_progress/completed/blocked"},
                "assignee": {"type": "string"},
                "add_blocks": {"type": "array", "items": {"type": "string"}},
                "add_blocked_by": {"type": "array", "items": {"type": "string"}},
                "remove_blocks": {"type": "array", "items": {"type": "string"}},
                "remove_blocked_by": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id"],
        }

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def category(self) -> str:
        return "task"

    async def execute(self, context: Any, input: dict) -> ToolResult:
        from core.team.tasks import Patch

        store = self._team_manager.get_task_store(self._resolve_team_name())
        if store is None:
            return ToolResult(success=False, error=f"未找到团队 '{self._team_name}'")
        patch = Patch(
            title=input.get("title"),
            description=input.get("description"),
            status=_parse_status(input.get("status")) if input.get("status") else None,
            assignee=input.get("assignee"),
            add_blocks=_str_list(input.get("add_blocks")),
            add_blocked_by=_str_list(input.get("add_blocked_by")),
            remove_blocks=_str_list(input.get("remove_blocks")),
            remove_blocked_by=_str_list(input.get("remove_blocked_by")),
        )
        task = await store.update(input.get("task_id", ""), patch)
        if task is None:
            return ToolResult(success=False, error="任务不存在")
        return ToolResult(success=True, data=task.to_dict())


def _parse_status(s: str):
    from core.team.tasks import Status

    return Status(s)


def _str_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    return [str(x) for x in v]


# ── SendMessage ──────────────────────────────────────────────────

VALID_MESSAGE_TYPES = {"text", "shutdown_request", "shutdown_response"}


class SendMessageTool(_TeamBoundMixin, Tool):
    """向队友点对点发消息（name / agent_id / '*' 广播）。仅团队成员持有。"""

    is_system_tool = True

    def __init__(
        self,
        team_manager: Any,
        team_name: str,
        from_agent_id: str,
        from_agent_name: str = "",
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._from_agent_id = from_agent_id
        self._from_agent_name = from_agent_name

    def name(self) -> str:
        return "SendMessage"

    def description(self) -> str:
        return (
            "Send a message to a teammate by name or agent id (to='*' broadcasts). "
            "Text messages require a 5-10 word summary."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "teammate name / agent id / '*'"},
                "summary": {"type": "string", "description": "5-10 word summary for text messages"},
                "message": {"type": "string"},
                "message_type": {"type": "string", "enum": sorted(VALID_MESSAGE_TYPES)},
                "metadata": {"type": "object"},
            },
            "required": ["to"],
        }

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"

    async def execute(self, context: Any, input: dict) -> ToolResult:
        from core.team.mailbox.message import Message, MessageType

        msg_type = input.get("message_type", "text")
        if msg_type not in VALID_MESSAGE_TYPES:
            return ToolResult(success=False, error=f"非法 message_type '{msg_type}'")
        if msg_type == "text" and not input.get("summary"):
            return ToolResult(success=False, error="text 消息必须带 summary（5-10 词）")

        team = self._team_manager.get(self._resolve_team_name())
        mailbox = self._team_manager.get_mailbox(self._resolve_team_name())
        if team is None or mailbox is None:
            return ToolResult(success=False, error=f"未找到团队 '{self._team_name}'")

        to = input.get("to", "")
        msg = Message(
            from_=self._from_agent_name or self._from_agent_id,
            to=to,
            type=MessageType(msg_type),
            summary=input.get("summary", ""),
            content=input.get("message", ""),
            payload=input.get("metadata"),
        )

        if to == "*":
            targets = [
                m.agent_id
                for m in team.members
                if m.agent_id != self._from_agent_id and m.agent_id != "lead"
            ]
            # Lead 也是广播对象（排除发件人自己即可）
            if team.lead_agent_id not in targets and team.lead_agent_id != self._from_agent_id:
                targets.append(team.lead_agent_id)
            for aid in targets:
                await mailbox.write(aid, msg)
            await self._wake_many(targets)
            return ToolResult(success=True, data=f"消息已广播给 {len(targets)} 人")

        target_id = await self._team_manager.resolve_agent_id(to)
        if target_id is None:
            return ToolResult(
                success=False, error=f"无法解析收件人 '{to}'（非本团队队友名/agent_id）"
            )
        delivered = {"delivered_to": [target_id]}
        await mailbox.write(target_id, msg)
        await self._wake_one(target_id)
        # F46：in-process 目标已 stop 时 → 从会话恢复续派
        await self._maybe_resume(target_id, msg.content)
        return ToolResult(success=True, data=delivered)

    async def _maybe_resume(self, agent_id: str, content: str) -> None:
        """目标为 in-process 队员且已 stop → 复用 BackgroundTaskManager.send_message 续派。"""
        member = self._member_by_agent_id(agent_id)
        if member is None:
            return
        bt = getattr(member.backend_type, "value", member.backend_type)
        if bt != "in-process":
            return  # pane 后端靠 wake 触发子进程读邮箱续派
        tm = getattr(self._team_manager, "task_mgr", None)
        if tm is None:
            return
        try:
            bt = tm.get(agent_id)
        except Exception:  # noqa: BLE001 —— 查询失败当作不可续派
            return
        if bt is not None:
            from core.task.manager import TaskStatus

            if bt.status is not TaskStatus.RUNNING:
                # 已空闲 → 置活跃并发起续派
                name = member.name
                try:
                    await self._team_manager.set_member_active(self._team(), name, True)
                except Exception as e:  # noqa: BLE001 —— 状态更新失败不阻断续派
                    logger.warning("member active update failed for %s: %s", name, e)
                await tm.send_message(name, content)

    def _team(self):
        return self._team_manager.get(self._resolve_team_name())

    async def _wake_one(self, agent_id: str) -> None:
        pane_id = self._team_manager.get_pane_id(agent_id)
        if not pane_id:
            return
        member = self._member_by_agent_id(agent_id)
        if member is None:
            return
        try:
            from core.team.backend import new_backend

            backend = new_backend(member.backend_type)
            await backend.wake(pane_id, agent_id)
        except Exception:  # noqa: BLE001 — pane 唤醒失败不阻断发信
            logger.warning("wake pane for %s failed", agent_id)

    async def _wake_many(self, agent_ids: list[str]) -> None:
        for aid in agent_ids:
            await self._wake_one(aid)

    def _member_by_agent_id(self, agent_id: str):
        team = self._team_manager.get(self._resolve_team_name())
        if team is None:
            return None
        return team.member_by_agent_id(agent_id)


# ── build_teammate_tools ─────────────────────────────────────────


def build_teammate_tools(
    parent_tools: list[Tool],
    team_manager: Any,
    team_name: str,
    agent_id: str,
    agent_name: str,
    backend_type: str = "in-process",
) -> list[Tool]:
    """构造队友的专用工具集（team-bound 协作工具 + 按后端收窄的基础工具）。

    参照参考实现 `build_teammate_tools`：
    - in-process 队友 → 基础工具白名单收窄到 IN_PROCESS_TEAMMATE_ALLOWED_TOOLS
    - pane 队友（tmux/iterm2）→ 保留父基础工具但去掉 TeamCreate/TeamDelete
    - 最后总把 5 个 team-bound 协作工具实例加入（与基础工具同名者以协cc覆盖）

    返回 Tool 对象列表，由注册方 register 进队友的专用 ToolRegistry。
    """
    collab = [
        TaskCreateTool(team_manager, team_name, agent_name),
        TaskGetTool(team_manager, team_name),
        TaskListTool(team_manager, team_name),
        TaskUpdateTool(team_manager, team_name),
        SendMessageTool(team_manager, team_name, agent_id, agent_name),
    ]
    collab_names = {t.name() for t in collab}

    if backend_type == "in-process":
        keep = [
            t for t in parent_tools
            if t.name() in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS
            and t.name() not in collab_names
        ]
    else:
        keep = [
            t for t in parent_tools
            if t.name() not in _TEAM_LEAD_ONLY_TOOLS
            and t.name() not in collab_names
        ]

    return keep + collab


__all__ = [
    "IN_PROCESS_TEAMMATE_ALLOWED_TOOLS",
    "SendMessageTool",
    "TaskCreateTool",
    "TaskGetTool",
    "TaskListTool",
    "TaskUpdateTool",
    "build_teammate_tools",
]
