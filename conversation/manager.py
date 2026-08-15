"""对话管理器。

维护单次会话内的完整对话历史，提供消息追加、API 格式转换、整体替换与重置能力。
支持工具调用消息。"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any, Optional

from conversation.history import check_alternating, filter_system
from conversation.message import (
    APIMessage,
    Message,
    MessageRole,
    MessageStatus,
    make_text_block,
    make_tool_result_block,
    make_tool_use_block,
)

logger = logging.getLogger(__name__)


class ConversationManager:
    """对话管理器，维护消息列表并提供格式转换。"""

    def __init__(
        self,
        system_prompt: str = "",
        on_append: Optional[Callable[[Message], None]] = None,
        on_replace: Optional[Callable[[list[Message]], None]] = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._messages: list[Message] = []
        self._on_append = on_append
        self._on_replace = on_replace

    def _emit_append(self, msg: Message) -> None:
        """提交后通知存档回调；回调失败不影响对话主流程。"""
        if self._on_append is not None:
            try:
                self._on_append(msg)
            except Exception:
                logger.exception("on_append 回调执行失败")

    def _emit_replace(self, msgs: list[Message]) -> None:
        """整体替换后通知存档回调。"""
        if self._on_replace is not None:
            try:
                self._on_replace(msgs)
            except Exception:
                logger.exception("on_replace 回调执行失败")

    def set_callbacks(
        self,
        on_append: Optional[Callable[[Message], None]] = None,
        on_replace: Optional[Callable[[list[Message]], None]] = None,
    ) -> None:
        """恢复会话后挂载存档回调（F22）。"""
        self._on_append = on_append
        self._on_replace = on_replace

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @system_prompt.setter
    def system_prompt(self, value: str) -> None:
        self._system_prompt = value

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def add_message(self, role: MessageRole, content: str) -> Message:
        """添加一条内部消息并返回。"""
        msg = Message(role=role, content=content, status=MessageStatus.COMPLETED)
        self._messages.append(msg)
        self._emit_append(msg)
        return msg

    def add_user_message(self, content: str) -> Message:
        """便捷方法：添加用户消息。"""
        return self.add_message(MessageRole.USER, content)

    def add_assistant_message(self, content: str) -> Message:
        """便捷方法：添加助手消息。"""
        return self.add_message(MessageRole.ASSISTANT, content)

    def add_tool_use(self, tool_use_id: str, name: str, input: dict[str, Any]) -> Message:
        """添加一条工具调用消息（assistant 角色，含 tool_use 元数据）。"""
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="",
            status=MessageStatus.COMPLETED,
            tool_use_id=tool_use_id,
            tool_name=name,
            tool_input=input,
        )
        self._messages.append(msg)
        self._emit_append(msg)
        return msg

    def add_tool_result(self, tool_use_id: str, content: str) -> Message:
        """添加一条工具结果消息（user 角色）。"""
        msg = Message(
            role=MessageRole.USER,
            content=content,
            status=MessageStatus.COMPLETED,
            tool_use_id=tool_use_id,
        )
        self._messages.append(msg)
        self._emit_append(msg)
        return msg

    def add_system_reminder(self, text: str) -> Message:
        """添加一条 system_reminder 消息（user 角色，带 [system_reminder] 包装）。

        用于在对话中间注入指令提示（如 Plan Mode 的迭代提醒），
        模型将其视为系统级指令，不影响角色交替。
        """
        return self.add_user_message(f"[system_reminder] {text}[/system_reminder]")

    def start_assistant_stream(self) -> Message:
        """创建一条流式中的助手消息，返回用于追加内容。"""
        msg = Message(role=MessageRole.ASSISTANT, content="", status=MessageStatus.STREAMING)
        self._messages.append(msg)
        return msg

    def append_to_stream(self, msg: Message, text: str) -> None:
        """向流式消息追加文本内容。"""
        msg.content += text

    def append_reasoning(self, msg: Message, text: str) -> None:
        """向流式消息追加思考内容（thinking mode）。

        思考文本单独存到 reasoning 字段，便于工具调用轮回传给
        需要回传 reasoning 的端点（如 DeepSeek 的 OpenAI 兼容接点）。
        """
        msg.reasoning += text

    def finish_stream(self, msg: Message, usage: Optional[dict] = None) -> None:
        """标记流式消息为完成状态。"""
        msg.status = MessageStatus.COMPLETED
        if usage:
            msg.usage = usage
        self._emit_append(msg)

    def fail_stream(self, msg: Message, error: str) -> None:
        """标记流式消息为错误状态。"""
        msg.status = MessageStatus.ERROR
        msg.content = error
        # 出错后 reasoning 已无意义（内容被替换为错误文本），清空避免残留进下一次
        # to_api_format（thinking 模式会回传 reasoning，残留会污染请求）。
        msg.reasoning = ""
        self._emit_append(msg)

    def to_api_format(self) -> tuple[list[APIMessage], str]:
        """将内部消息转为 API 请求格式。

        返回 (messages, system_prompt)：
        - messages: 适合发送给 LLM 的消息列表（支持内容块）
        - system_prompt: 独立的 system prompt

        将相邻的同角色文本消息合并，正确序列化工具调用消息。
        """
        api_messages: list[APIMessage] = []
        for m in self._messages:
            if m.tool_use_id and m.role == MessageRole.USER:
                # tool_result 消息 → content 为 list[dict]
                api_messages.append(
                    APIMessage(
                        role=m.role.value,
                        content=[make_tool_result_block(m.tool_use_id, m.content)],
                    )
                )
            elif m.tool_use_id and m.role == MessageRole.ASSISTANT:
                # tool_use 消息 → content 为 list[dict]
                blocks = []
                if m.content:
                    blocks.append(make_text_block(m.content))
                if m.tool_name and m.tool_input is not None:
                    blocks.append(make_tool_use_block(m.tool_use_id, m.tool_name, m.tool_input))
                api_messages.append(
                    APIMessage(
                        role=m.role.value,
                        content=blocks,
                        reasoning=m.reasoning,
                    )
                )
            else:
                api_messages.append(m.to_api())

        # 合并相邻同角色（仅对纯文本消息）
        merged = _merge_content_blocks(api_messages)
        merged = filter_system(merged)
        check_alternating(merged)
        return merged, self._system_prompt

    def reset(self) -> None:
        """清空消息列表（保留 system prompt）。"""
        self._messages.clear()

    @property
    def messages(self) -> list[Message]:
        """返回当前消息列表的浅拷贝。"""
        return list(self._messages)

    def replace_history(self, msgs: list[Message]) -> None:
        """整体替换内存消息列表。

        compact 摘要后调用此方法一次性丢弃旧历史并装入
        "摘要 + 恢复 + 近期原文"。

        做深拷贝确保外部后续修改不影响内部状态。
        """
        self._messages = copy.deepcopy(msgs) if msgs else []
        self._emit_replace(self._messages)


def _merge_content_blocks(messages: list[APIMessage]) -> list[APIMessage]:
    """合并相邻同角色的消息，将 content 转为内容块列表。

    用于将连续的 assistant 消息（文本 + tool_use）合并为一条
    content blocks 消息，符合 Anthropic API 格式。
    """
    if not messages:
        return []

    result = [messages[0]]
    for msg in messages[1:]:
        prev = result[-1]
        if prev.role == msg.role:
            prev.content = _as_blocks(prev.content) + _as_blocks(msg.content)
            # 合并思考文本（thinking mode 回传）
            if msg.reasoning:
                prev.reasoning = (prev.reasoning + "\n" + msg.reasoning).strip()
        else:
            result.append(msg)
    return result


def _as_blocks(content: str | list[dict]) -> list[dict]:
    """Convert str or list[dict] content to a flat list of content blocks."""
    if isinstance(content, list):
        return content
    if content:
        return [make_text_block(content)]
    return []
