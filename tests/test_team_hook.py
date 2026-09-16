"""TeammateContext 单元测试。

覆盖：ContextVar 出入栈、current_teammate 读取、闭包注入的 mailbox 访问、
IncomingMessage 视图。
"""

from __future__ import annotations

from core.agent.team_hook import (
    IncomingMessage,
    TeammateContext,
    current_teammate,
)


def _make_tc():
    async def read():
        return [0, 1], [
            IncomingMessage(from_="lead", to="alice", type="text", summary="m1",
                            content="one"),
            IncomingMessage(from_="bob", to="alice", type="text", summary="m2",
                            content="two"),
        ]

    marked: list[list[int]] = []

    async def mark(indices):
        marked.append(list(indices))

    return TeammateContext(
        team_name="demo",
        member_name="alice",
        agent_id="agent-a",
        read_unread=read,
        mark_read=mark,
    ), marked


def test_install_uninstall_context():
    tc, _ = _make_tc()
    assert current_teammate() is None
    tc.install()
    assert current_teammate() is tc
    assert current_teammate().member_name == "alice"
    tc.uninstall()
    assert current_teammate() is None


def test_incoming_message_fields():
    m = IncomingMessage(from_="lead", to="alice", type="plan_approval_response",
                        summary="ok", payload={"approve": True})
    assert m.payload == {"approve": True}


async def test_read_unread_and_mark_closures():
    tc, marked = _make_tc()
    tc.install()
    try:
        indices, msgs = await current_teammate().read_unread()
        assert indices == [0, 1]
        assert [m.summary for m in msgs] == ["m1", "m2"]
        await current_teammate().mark_read([1])
        assert marked == [[1]]
    finally:
        tc.uninstall()


async def test_default_noop_closures():
    """未注入闭包时 read_unread 返回空、mark_read 不抛错。"""
    tc = TeammateContext(team_name="t", member_name="m", agent_id="a")
    indices, msgs = await tc.read_unread()
    assert indices == [] and msgs == []
    await tc.mark_read([0])  # 不抛错
