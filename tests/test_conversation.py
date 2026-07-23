"""对话管理器和历史测试。"""

from __future__ import annotations

import pytest

from conversation.manager import ConversationManager
from conversation.message import MessageRole, MessageStatus
from conversation.history import (
    check_alternating,
    filter_system,
    merge_consecutive_same_role,
)
from conversation.message import APIMessage


class TestHistory:
    """历史工具函数。"""

    def test_check_alternating_valid(self):
        msgs = [
            APIMessage("user", "hi"),
            APIMessage("assistant", "hello"),
            APIMessage("user", "how are you"),
        ]
        check_alternating(msgs)  # should not raise

    def test_check_alternating_with_system_first(self):
        msgs = [
            APIMessage("system", "sys1"),
            APIMessage("system", "sys2"),
            APIMessage("user", "hi"),
            APIMessage("assistant", "hello"),
        ]
        check_alternating(msgs)  # system can be consecutive at start

    def test_check_alternating_consecutive_user(self):
        msgs = [
            APIMessage("user", "hi"),
            APIMessage("user", "hi again"),
        ]
        with pytest.raises(ValueError, match="交替"):
            check_alternating(msgs)

    def test_check_alternating_consecutive_assistant(self):
        msgs = [
            APIMessage("user", "hi"),
            APIMessage("assistant", "hello"),
            APIMessage("assistant", "world"),
        ]
        with pytest.raises(ValueError, match="交替"):
            check_alternating(msgs)

    def test_filter_system(self):
        msgs = [
            APIMessage("system", "sys"),
            APIMessage("user", "hi"),
            APIMessage("assistant", "hello"),
        ]
        filtered = filter_system(msgs)
        assert len(filtered) == 2
        assert filtered[0].role == "user"

    def test_merge_consecutive_same_role(self):
        msgs = [
            APIMessage("system", "s1"),
            APIMessage("system", "s2"),
            APIMessage("user", "hi"),
        ]
        merged = merge_consecutive_same_role(msgs)
        assert len(merged) == 2
        assert merged[0].content == "s1\ns2"

    def test_merge_noop_with_alternating(self):
        msgs = [
            APIMessage("user", "hi"),
            APIMessage("assistant", "hello"),
        ]
        merged = merge_consecutive_same_role(msgs)
        assert len(merged) == 2


class TestConversationManager:
    """对话管理器集成行为。"""

    def test_empty_initial(self):
        cm = ConversationManager("sys prompt")
        assert cm.system_prompt == "sys prompt"
        assert cm.messages == []

    def test_add_user_and_assistant(self):
        cm = ConversationManager()
        u = cm.add_user_message("Hello")
        a = cm.add_assistant_message("Hi!")
        assert u.role == MessageRole.USER
        assert a.role == MessageRole.ASSISTANT
        assert len(cm.messages) == 2

    def test_stream_workflow(self):
        cm = ConversationManager()
        cm.add_user_message("Hello")
        s = cm.start_assistant_stream()
        assert s.status == MessageStatus.STREAMING
        cm.append_to_stream(s, "Hello, ")
        cm.append_to_stream(s, "world!")
        cm.finish_stream(s, {"output_tokens": 2})
        assert s.content == "Hello, world!"
        assert s.status == MessageStatus.COMPLETED
        assert s.usage == {"output_tokens": 2}

    def test_fail_stream(self):
        cm = ConversationManager()
        cm.add_user_message("Hello")
        s = cm.start_assistant_stream()
        cm.fail_stream(s, "API error")
        assert s.status == MessageStatus.ERROR
        assert s.content == "API error"

    def test_to_api_format(self):
        cm = ConversationManager("You are helpful.")
        cm.add_user_message("Hi")
        cm.add_assistant_message("Hello")
        api_msgs, system = cm.to_api_format()
        assert system == "You are helpful."
        assert len(api_msgs) == 2
        assert api_msgs[0].role == "user"
        assert api_msgs[1].role == "assistant"

    def test_to_api_format_merges_consecutive_user(self):
        """连续相同角色的消息会被合并而非报错。"""
        cm = ConversationManager()
        cm.add_user_message("Hi")
        cm.add_user_message("Hello again")
        api_msgs, _ = cm.to_api_format()
        assert len(api_msgs) == 1
        # Merged into content blocks (not plain string)
        content = api_msgs[0].content
        assert isinstance(content, list)
        texts = [b["text"] for b in content if b.get("type") == "text"]
        assert texts == ["Hi", "Hello again"]

    def test_reset(self):
        cm = ConversationManager("sys")
        cm.add_user_message("Hi")
        cm.add_assistant_message("Hello")
        cm.reset()
        assert cm.messages == []
        assert cm.system_prompt == "sys"
