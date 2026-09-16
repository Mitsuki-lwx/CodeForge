"""队员邮箱注入单测。

覆盖：build_incoming_reminder 格式、ingest_team_mailbox 读未读+mark_read、
plan_approval_response 处理。
"""

from __future__ import annotations

from core.agent.team_hook import IncomingMessage, TeammateContext
from core.agent.team_mailbox import build_incoming_reminder, ingest_team_mailbox


async def _noop_mark(indices):
    pass


def test_build_incoming_reminder():
    msgs = [
        IncomingMessage(from_="lead", to="alice", type="text", summary="hi",
                        content="hello there"),
        IncomingMessage(from_="bob", to="alice", type="text", summary="done",
                        content="done the work"),
    ]
    text = build_incoming_reminder(msgs)
    assert "<incoming-messages>" in text
    assert "收到 2 条新消息" in text
    assert "来自 lead" in text
    assert "hello there" in text


def test_build_incoming_reminder_content_truncated():
    msgs = [IncomingMessage(from_="lead", to="a", type="text", summary="long",
                            content="x" * 500)]
    text = build_incoming_reminder(msgs)
    assert "xxx" in text
    assert len(text) < 500 + 200  # content 截断到 200


async def test_ingest_reads_unread_and_marks_read():
    marked: list[list[int]] = []

    async def read():
        return [0, 1], [
            IncomingMessage(from_="lead", to="a", type="text", summary="m1", content="one"),
            IncomingMessage(from_="lead", to="a", type="text", summary="m2", content="two"),
        ]

    async def mark(indices):
        marked.append(list(indices))

    tc = TeammateContext(team_name="t", member_name="a", agent_id="agent-a",
                         read_unread=read, mark_read=mark)
    reminders = await ingest_team_mailbox(tc)
    assert len(reminders) == 1
    assert "<incoming-messages>" in reminders[0]
    assert marked == [[0, 1]]


async def test_ingest_empty():
    async def read():
        return [], []

    tc = TeammateContext(team_name="t", member_name="a", agent_id="agent-a",
                         read_unread=read, mark_read=_noop_mark)
    assert await ingest_team_mailbox(tc) == []


async def test_plan_approval_true():
    async def read():
        return [0], [IncomingMessage(
            from_="lead", to="p", type="plan_approval_response",
            summary="ok", payload={"approve": True})]

    tc = TeammateContext(team_name="t", member_name="p", agent_id="agent-p",
                         read_unread=read, mark_read=_noop_mark)
    reminders = await ingest_team_mailbox(tc)
    assert any("已批准" in r for r in reminders)


async def test_plan_approval_false_feedback():
    async def read():
        return [0], [IncomingMessage(
            from_="lead", to="p", type="plan_approval_response",
            summary="reject", payload={"approve": False, "feedback": "再想想"})]

    tc = TeammateContext(team_name="t", member_name="p", agent_id="agent-p",
                         read_unread=read, mark_read=_noop_mark)
    reminders = await ingest_team_mailbox(tc)
    assert any("驳回了" in r and "再想想" in r for r in reminders)
