"""邮箱单元测试。

覆盖：write/read round-trip、timestamp 自动补、默认未读、read_unread + mark_read、
并发 10 个 task 写不丢消息。
"""

from __future__ import annotations

import asyncio

from core.team.mailbox import Box
from core.team.mailbox.message import Message, MessageType


async def test_write_read_round_trip(tmp_path):
    box = Box(str(tmp_path))
    await box.write(
        "alice",
        Message(from_="lead", to="alice", summary="hi", content="hello"),
    )
    msgs = await box.read("alice")
    assert len(msgs) == 1
    m = msgs[0]
    assert m.from_ == "lead"
    assert m.to == "alice"
    assert m.content == "hello"
    assert m.read is False  # 默认未读
    assert m.type is MessageType.TEXT
    assert m.timestamp > 0  # 自动补时间戳


async def test_timestamp_defaulted_on_write(tmp_path):
    box = Box(str(tmp_path))
    await box.write("bob", Message(from_="lead", to="bob", summary="x", content=""))
    msgs = await box.read("bob")
    assert msgs[0].timestamp > 0


async def test_read_unread_and_mark_read(tmp_path):
    box = Box(str(tmp_path))
    await box.write("a", Message(from_="lead", to="a", summary="m1", content="one"))
    await box.write("a", Message(from_="lead", to="a", summary="m2", content="two"))

    indices, unread = await box.read_unread("a")
    assert indices == [0, 1]
    assert [u.summary for u in unread] == ["m1", "m2"]

    await box.mark_read("a", [0])
    indices2, unread2 = await box.read_unread("a")
    assert indices2 == [1]
    assert unread2[0].summary == "m2"
    # 读回全部：第一条已读，第二条未读
    all_msgs = await box.read("a")
    assert all_msgs[0].read is True
    assert all_msgs[1].read is False


async def test_concurrent_writes_no_loss(tmp_path):
    """并发 10 个 task 写同一收件人，10 条消息全部落盘无丢失。"""
    box = Box(str(tmp_path))
    await asyncio.gather(
        *[
            box.write(
                "a",
                Message(from_=f"m{i}", to="a", summary=f"s{i}", content=f"c{i}"),
            )
            for i in range(10)
        ]
    )
    msgs = await box.read("a")
    assert len(msgs) == 10
    from_ = sorted(m.from_ for m in msgs)
    assert from_ == [f"m{i}" for i in range(10)]


async def test_plan_approval_message_type(tmp_path):
    box = Box(str(tmp_path))
    await box.write(
        "planner",
        Message(
            from_="lead",
            to="planner",
            type=MessageType.PLAN_APPROVAL_RESPONSE,
            summary="approved",
            payload={"approve": True, "feedback": ""},
        ),
    )
    msgs = await box.read("planner")
    m = msgs[0]
    assert m.type is MessageType.PLAN_APPROVAL_RESPONSE
    assert m.payload == {"approve": True, "feedback": ""}
