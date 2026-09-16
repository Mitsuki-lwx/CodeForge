"""pane 队友自治循环单测（T29）。

用 fake factory + monkeypatch run_to_completion，验证主循环：
读 text 消息 → run → 通知 Lead idle → 标 inactive；遇 shutdown_request 退出。
"""

from __future__ import annotations

import core.agent.sub_agent as sub_agent_mod
from core.team.mailbox import Box
from core.team.mailbox.message import Message, MessageType
from core.team.team_member import run_team_member


class _FakeManager:
    def __init__(self, tmp_path) -> None:
        from core.team.types import Team

        self.team = Team(name="demo", sanitized_name="demo")
        self.team.mailbox_dir = str(tmp_path / "mailbox")
        self.active_changes: list[tuple] = []

    def get(self, name):
        return self.team if name == "demo" else None

    async def set_member_active(self, team, name, active):
        self.active_changes.append((name, active))


async def test_run_team_member_text_then_shutdown(tmp_path, monkeypatch):
    mgr = _FakeManager(tmp_path)
    box = Box(mgr.team.mailbox_dir)
    # 先写 text 任务，再写 shutdown → 跑一次后退出，避免测循环卡在超时
    await box.write("agent-a", Message(from_="lead", to="a", type=MessageType.TEXT,
                                       summary="do work", content="do the task"))
    await box.write("agent-a", Message(from_="lead", to="a", type=MessageType.SHUTDOWN_REQUEST,
                                       summary="stop", content=""))

    ran: list[str] = []

    async def fake_rtc(agent, conv, task="", events=None):
        ran.append("ran")
        return "done"

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_rtc)

    def factory(wt, member, agent_id, prompt):
        return object(), object()

    await run_team_member(
        manager=mgr, team_name="demo", member_name="alice", agent_id="agent-a",
        worktree_path="/wt", agent_factory=factory,
    )

    # 跑了一次 + 退出
    assert ran == ["ran"]
    # Lead 收到 idle 通知
    lead_msgs = await box.read("lead")
    assert any("idle" in m.summary for m in lead_msgs)
    # 成员标 inactive
    assert ("alice", False) in mgr.active_changes


async def test_run_team_member_unknown_team(tmp_path):
    mgr = _FakeManager(tmp_path)
    # 不存在的 team → 静默返回不抛
    await run_team_member(
        manager=mgr, team_name="nope", member_name="a", agent_id="agent-a",
        worktree_path="/wt",
    )
