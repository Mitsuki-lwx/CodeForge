"""Token 估算。

纯函数，不依赖外部状态。

估算策略：锚定最近一次主对话 provider usage + 之后新增消息的字符增量。
所有用量估算使用 len(content.encode("utf-8")) / ESTIMATE_CHARS_PER_TOKEN 近似。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Protocol

from core.context_compression.const import ESTIMATE_CHARS_PER_TOKEN


class HasContent(Protocol):
    """`message_chars` 需要的全部接口 —— 只读 `.content`。

    刻意不用 `list[Message]`：本函数同时被喂 `Message`（会话内部）与
    `APIMessage`（`to_api_format()` 的产物），两者只共有 `.content`。
    写死具体类型会让调用点不得不转型（mypy 报 arg-type），
    而这里真正依赖的只有 `.content` 一个属性。

    声明成**只读 property**（不是普通属性）：普通属性对 Protocol 是**不变**的，
    `Message.content: str` 就不满足 `str | list[dict]`；只读 property 才是协变的。
    """

    @property
    def content(self) -> str | list[dict[str, Any]]: ...


def usage_anchor(u: dict[str, Any]) -> int:
    """把 stream 尾事件中的 usage 合并成单一锚点值。

    等价于 input_tokens + output_tokens + cache_read + cache_write。
    """
    return (
        u.get("input_tokens", 0)
        + u.get("output_tokens", 0)
        + u.get("cache_read_input_tokens", 0)
        + u.get("cache_creation_input_tokens", 0)
    )


def message_chars(msgs: Sequence[HasContent]) -> int:
    """计算消息列表的字符总量（UTF-8 字节）。

    累加每条消息 content 的字节长度。
    content 可以是 str 或 list[dict]（内容块列表）。
    """
    total = 0
    for m in msgs:
        total += _content_chars(m.content)
    return total


def _content_chars(content: str | list[dict[str, Any]]) -> int:
    """计算单条消息 content 的 UTF-8 字节长度。"""
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    # content block list
    chars = 0
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                chars += len(block.get("text", "").encode("utf-8"))
            elif block.get("type") == "tool_use":
                import json

                chars += len(
                    json.dumps(block.get("input", {}), ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
            elif block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    chars += len(result_content.encode("utf-8"))
                elif isinstance(result_content, list):
                    for item in result_content:
                        if isinstance(item, dict) and "text" in item:
                            chars += len(item["text"].encode("utf-8"))
    return chars


def estimate_tokens(
    anchor: int,
    all_msgs: Sequence[HasContent],
    anchor_msg_len: int,
) -> int:
    """锚定真实 usage + 字符增量估算。

    入参语义：
      - anchor: 上一次主对话路径 stream 真实 usage 之和
      - all_msgs: 会话完整消息列表（必须已过 layer1 处理）
      - anchor_msg_len: anchor 被记录时 conv 的消息条数
        函数只计算 all_msgs[anchor_msg_len:] 这部分的字符增量，
        避免把已含在 anchor 里的历史重复计算。

    返回 int 类型（math.ceil 结果）。
    """
    start = max(0, anchor_msg_len)
    tail = all_msgs[start:] if start < len(all_msgs) else []
    if not tail:
        return anchor
    return anchor + math.ceil(message_chars(tail) / ESTIMATE_CHARS_PER_TOKEN)
