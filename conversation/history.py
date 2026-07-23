"""消息列表维护与交替校验。"""

from __future__ import annotations

from conversation.message import APIMessage, Message, MessageRole


def check_alternating(messages: list[APIMessage]) -> None:
    """校验 user/assistant 消息是否交替排列（允许连续的 system 消息在前）。

    若检测到连续两个 user 或两个 assistant 消息，抛出 ValueError。
    """
    prev: str | None = None
    for msg in messages:
        if msg.role == "system":
            prev = None  # system 后重置，下一个任意角色均可
            continue
        if prev is not None and msg.role == prev:
            raise ValueError(
                f"消息交替校验失败：连续两个 '{msg.role}' 消息"
            )
        prev = msg.role


def merge_consecutive_same_role(messages: list[APIMessage]) -> list[APIMessage]:
    """合并连续同角色的消息（通常不应用 user/assistant 之间）。

    主要用于开头连续的 system 消息合并为一条。
    """
    if not messages:
        return []

    result: list[APIMessage] = [messages[0]]
    for msg in messages[1:]:
        if msg.role == result[-1].role:
            result[-1] = APIMessage(
                role=msg.role,
                content=result[-1].content + "\n" + msg.content,
            )
        else:
            result.append(msg)
    return result


def filter_system(messages: list[APIMessage]) -> list[APIMessage]:
    """过滤出非 system 消息（用于 API 请求的 messages 数组）。"""
    return [m for m in messages if m.role != "system"]
