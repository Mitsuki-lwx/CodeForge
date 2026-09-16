"""团队协作工具 + build_teammate_tools 单元测试。

用 fake TeamManager（实现 get/get_mailbox/get_task_store/resolve_agent_id/...）验证：
- build_teammate_tools 对 in-process 白名单收窄、对 pane 移除 TeamCreate/TeamDelete；
- 5 个协作工具总是被注入；
- 各工具的 execute 正常/错误路径（TaskCreate/TaskGet/TaskList/TaskUpdate/SendMessage）。
"""

from __future__ import annotations

from core.team.mailbox import Box
from core.team.mailbox.message import MessageType
from core.team.tasks import Store
from core.team.tools import (
    SendMessageTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    build_teammate_tools,
)
from core.team.types import TeammateInfo
from core.tool.interface import Tool
from core.tool.result import ToolResult


class _FakeManager:
    """模拟 TeamManager：面向协作工具的接口。"""

    def __init__(self, tmp_path) -> None:
        from core.team.types import Team

        self.team = Team(name="demo", sanitized_name="demo")
        self.team.mailbox_dir = str(tmp_path / "mailbox")
        self.team.tasks_path = str(tmp_path / "tasks.json")
        self.registry_names: dict[str, str] = {}

    def get(self, name):
        return self.team if name == "demo" else None

    def get_mailbox(self, name):
        return Box(self.team.mailbox_dir) if name == "demo" else None

    def get_task_store(self, name):
        return Store(self.team.tasks_path) if name == "demo" else None

    def get_pane_id(self, agent_id):
        m = self.team.member_by_agent_id(agent_id)
        return (m.pane_id or None) if m else None

    async def resolve_agent_id(self, name_or_id):
        if name_or_id in self.registry_names:
            return self.registry_names[name_or_id]
        m = self.team.member_by_name(name_or_id)
        if m:
            return m.agent_id
        m = self.team.member_by_agent_id(name_or_id)
        return m.agent_id if m else None


class _PlaceholderTool(Tool):
    """用于 build_teammate_tools 的父注册表占位工具。"""

    def __init__(self, nm: str) -> None:
        self._nm = nm

    def name(self) -> str:
        return self._nm

    def description(self) -> str:
        return self._nm

    def input_schema(self) -> dict:
        return {}

    async def execute(self, ctx, inp):
        return ToolResult(success=True, data="")

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, inp) -> bool:
        return True

    def category(self) -> str:
        return "read"


def _parent_tools():
    names = [
        "read_file", "write_file", "glob", "grep", "bash",
        "TeamCreate", "TaskList", "SendMessage", "TaskGet", "load_skill",
    ]
    return [_PlaceholderTool(n) for n in names]


def test_build_teammate_tools_inprocess_whitelist(tmp_path):
    mgr = _FakeManager(tmp_path)
    tools = build_teammate_tools(
        parent_tools=_parent_tools(), team_manager=mgr, team_name="demo",
        agent_id="agent-a", agent_name="alice", backend_type="in-process",
    )
    names = [t.name() for t in tools]
    for c in ("TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "SendMessage"):
        assert c in names
    assert "TeamCreate" not in names
    assert "read_file" in names
    assert "write_file" in names


def test_build_teammate_tools_pane_keeps_parent_minus_teamcreate(tmp_path):
    mgr = _FakeManager(tmp_path)
    tools = build_teammate_tools(
        parent_tools=_parent_tools(), team_manager=mgr, team_name="demo",
        agent_id="agent-a", agent_name="alice", backend_type="tmux",
    )
    names = [t.name() for t in tools]
    assert "write_file" in names
    assert "TeamCreate" not in names
    assert "SendMessage" in names


def test_build_teammate_tools_excludes_duplicate_collab_names(tmp_path):
    """队友 registry 里每个协作工具只出现一次（team-bound 实例覆盖父占位）。"""
    mgr = _FakeManager(tmp_path)
    tools = build_teammate_tools(
        parent_tools=_parent_tools(), team_manager=mgr, team_name="demo",
        agent_id="agent-a", agent_name="alice", backend_type="tmux",
    )
    names = [t.name() for t in tools]
    for c in ("TaskList", "SendMessage", "TaskGet"):
        assert names.count(c) == 1


async def test_task_create_and_list(tmp_path):
    mgr = _FakeManager(tmp_path)
    create = TaskCreateTool(mgr, "demo", "alice")
    res = await create.execute(None, {"title": "写 README", "assignee": "alice"})
    assert res.success
    tid = res.data
    assert tid.startswith("task_")

    lst = TaskListTool(mgr, "demo")
    res = await lst.execute(None, {})
    assert res.success
    assert len(res.data) == 1
    assert res.data[0]["title"] == "写 README"

    get = TaskGetTool(mgr, "demo")
    res = await get.execute(None, {"task_id": tid})
    assert res.success
    assert res.data["assignee"] == "alice"


async def test_task_update_dependency(tmp_path):
    mgr = _FakeManager(tmp_path)
    create = TaskCreateTool(mgr, "demo", "alice")
    b_id = (await create.execute(None, {"title": "前置"})).data
    a_id = (await create.execute(None, {"title": "A 依赖 B"})).data
    upd = TaskUpdateTool(mgr, "demo")
    await upd.execute(None, {"task_id": a_id, "add_blocked_by": [b_id]})
    get = TaskGetTool(mgr, "demo")
    a = (await get.execute(None, {"task_id": a_id})).data
    b = (await get.execute(None, {"task_id": b_id})).data
    assert b_id in a["blocked_by"]
    assert a_id in b["blocks"]


async def test_send_message_delivers_to_mailbox(tmp_path):
    mgr = _FakeManager(tmp_path)
    mgr.team.members = [
        TeammateInfo(name="lead", agent_id="lead"),
        TeammateInfo(name="alice", agent_id="agent-a"),
    ]
    mgr.registry_names["alice"] = "agent-a"
    sm = SendMessageTool(mgr, "demo", "lead", "lead")
    res = await sm.execute(None, {"to": "alice", "message": "hello", "summary": "say hi"})
    assert res.success
    box = Box(mgr.team.mailbox_dir)
    msgs = await box.read("agent-a")
    assert len(msgs) == 1
    assert msgs[0].content == "hello"
    assert msgs[0].type is MessageType.TEXT


async def test_send_message_requires_summary_for_text(tmp_path):
    mgr = _FakeManager(tmp_path)
    sm = SendMessageTool(mgr, "demo", "lead", "lead")
    res = await sm.execute(None, {"to": "alice", "message": "hello"})  # 无 summary
    assert res.success is False


async def test_send_message_broadcast(tmp_path):
    mgr = _FakeManager(tmp_path)
    mgr.team.members = [
        TeammateInfo(name="lead", agent_id="lead"),
        TeammateInfo(name="alice", agent_id="agent-a"),
        TeammateInfo(name="bob", agent_id="agent-b"),
    ]
    sm = SendMessageTool(mgr, "demo", "lead", "lead")
    res = await sm.execute(None, {"to": "*", "message": "hi", "summary": "broadcast"})
    assert res.success
    box = Box(mgr.team.mailbox_dir)
    assert len(await box.read("agent-a")) == 1
    assert len(await box.read("agent-b")) == 1
