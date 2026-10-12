"""消息列表维护与交替校验。"""

from __future__ import annotations

from conversation.message import APIMessage


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
            raise ValueError(f"消息交替校验失败：连续两个 '{msg.role}' 消息")
        prev = msg.role


def filter_system(messages: list[APIMessage]) -> list[APIMessage]:
    """过滤出非 system 消息（用于 API 请求的 messages 数组）。"""
    return [m for m in messages if m.role != "system"]
