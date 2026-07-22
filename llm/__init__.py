"""LLM 客户端子包。

导出 LLMClient 抽象基类、流式事件类型、以及 PromptTooLongError 哨兵异常。
"""

from __future__ import annotations


class PromptTooLongError(Exception):
    """Provider 上报上下文超出窗口时统一抛出的哨兵异常。

    Anthropic: BadRequestError + message "prompt is too long"
    OpenAI:   BadRequestError + code "context_length_exceeded"

    各协议 provider 将原始 SDK 异常包装为此类型，
    原始异常可通过 __cause__ 访问。
    """
