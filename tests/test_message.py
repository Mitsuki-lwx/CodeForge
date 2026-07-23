"""消息模型测试。"""

from __future__ import annotations

import time

from conversation.message import APIMessage, Message, MessageRole, MessageStatus


class TestMessage:
    """内部消息模型。"""

    def test_default_fields(self):
        msg = Message(role=MessageRole.USER, content="hello")
        assert msg.role == MessageRole.USER
        assert msg.content == "hello"
        assert len(msg.id) == 12
        assert msg.status == MessageStatus.PENDING
        assert isinstance(msg.timestamp, float)
        assert msg.usage is None

    def test_to_api(self):
        msg = Message(role=MessageRole.USER, content="hello")
        api = msg.to_api()
        assert api.role == "user"
        assert api.content == "hello"

    def test_assistant_role_value(self):
        msg = Message(role=MessageRole.ASSISTANT, content="hi")
        assert msg.to_api().role == "assistant"

    def test_system_role_value(self):
        msg = Message(role=MessageRole.SYSTEM, content="sys")
        assert msg.to_api().role == "system"

    def test_status_transitions(self):
        msg = Message(role=MessageRole.USER, content="", status=MessageStatus.STREAMING)
        assert msg.status == MessageStatus.STREAMING
        msg.status = MessageStatus.COMPLETED
        assert msg.status == MessageStatus.COMPLETED

    def test_timestamp_on_creation(self):
        before = time.time()
        msg = Message(role=MessageRole.USER, content="t")
        after = time.time()
        assert before <= msg.timestamp <= after

    def test_unique_ids(self):
        ids = {Message(role=MessageRole.USER, content="").id for _ in range(100)}
        assert len(ids) == 100  # all unique


class TestAPIMessage:
    """API 层消息。"""

    def test_fields(self):
        msg = APIMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
