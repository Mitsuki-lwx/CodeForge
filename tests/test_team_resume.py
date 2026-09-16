"""SendMessage 续写检测单测（F46）。

in-process 目标已 stop 时，SendMessage 应触发 task_mgr.send_message 续派 + 置活跃。
pane/missing task 目标不续派。
"""

from __future__ import annotations

from core.task.manager import TaskStatus
from core.team.mailbox import Box
from core.team.tools import SendMessageTool
from core.team.types import TeammateInfo


class _StoppedTask:
    status = TaskStatus.COMPLETED


class _TaskMgr:
    def __init__(self) -> None:
        self.resumed: list[tuple] = []
        self._by_id = {}

    def get(self, agent_id):
        return self._by_id.get(agent_id)

    async def send_message(self, name, message):
        self.resumed.append((name, message))
        return "task_x1"


class _FakeManager:
    def __init__(self, tmp_path, task_mgr) -> None:
        from core.team.types import Team

        self.team = Team(name="demo", sanitized_name="demo")
        self.team.mailbox_dir = str(tmp_path / "mailbox")
        self.team.tasks_path = str(tmp_path / "tasks.json")
        self.task_mgr = task_mgr
        self.active_changes: list[tuple] = []

    def get(self, name):
        return self.team if name == "demo" else None

    def get_mailbox(self, name):
        return Box(self.team.mailbox_dir) if name == "demo" else None

    async def set_member_active(self, team, name, active):
        self.active_changes.append((name, active))

    async def resolve_agent_id(self, name_or_id):
        m = self.team.member_by_name(name_or_id)
        return m.agent_id if m else name_or_id

    def get_pane_id(self, agent_id):
        m = self.team.member_by_agent_id(agent_id)
        return (m.pane_id or None) if m else None


async def test_send_message_resumes_stopped_inprocess(tmp_path):
    task_mgr = _TaskMgr()
    task_mgr._by_id["agent-a"] = _StoppedTask()
    mgr = _FakeManager(tmp_path, task_mgr)
    mgr.team.members = [
        TeammateInfo(name="lead", agent_id="lead", backend_type="in-process"),
        TeammateInfo(name="alice", agent_id="agent-a", backend_type="in-process"),
    ]
    sm = SendMessageTool(mgr, "demo", "lead", "lead")
    res = await sm.execute(None, {"to": "alice", "message": "continue", "summary": "next task"})
    assert res.success
    # 已 stop → 续派 + 置活跃
    assert task_mgr.resumed == [("alice", "continue")]
    assert ("alice", True) in mgr.active_changes


async def test_send_message_does_not_resume_running(tmp_path):
    class _RunningTask:
        status = TaskStatus.RUNNING

    task_mgr = _TaskMgr()
    task_mgr._by_id["agent-a"] = _RunningTask()
    mgr = _FakeManager(tmp_path, task_mgr)
    mgr.team.members = [
        TeammateInfo(name="lead", agent_id="lead", backend_type="in-process"),
        TeammateInfo(name="alice", agent_id="agent-a", backend_type="in-process"),
    ]
    sm = SendMessageTool(mgr, "demo", "lead", "lead")
    await sm.execute(None, {"to": "alice", "message": "x", "summary": "y"})
    assert task_mgr.resumed == []  # running 不续派


async def test_send_message_does_not_resume_pane(tmp_path):
    task_mgr = _TaskMgr()
    task_mgr._by_id["agent-b"] = _StoppedTask()
    mgr = _FakeManager(tmp_path, task_mgr)
    mgr.team.members = [
        TeammateInfo(name="lead", agent_id="lead", backend_type="in-process"),
        TeammateInfo(name="bob", agent_id="agent-b", backend_type="tmux"),
    ]
    sm = SendMessageTool(mgr, "demo", "lead", "lead")
    await sm.execute(None, {"to": "bob", "message": "x", "summary": "y"})
    assert task_mgr.resumed == []  # pane 靠 wake，不续派
