"""Anthropic Messages 协议客户端。

薄委托层：持有 .config，按其协商出的 Anthropic Session 执行流式对话。
wire 封装/解析已迁至 llm.adapters.anthropic。

`AnthropicClient` 名称、构造函数 (config)、`.config`、`stream_chat` 签名均保持，
`DEFAULT_ANTHROPIC_BASE_URL` / `_format_tool` 从适配器 re-export 以兼容测试。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.adapters.anthropic import (
    DEFAULT_ANTHROPIC_BASE_URL,
    AnthropicAdapter,
    _format_tool,
)
from llm.client import LLMClient
from llm.session import Session
from llm.stream_events import StreamEvent

__all__ = [
    "DEFAULT_ANTHROPIC_BASE_URL",
    "AnthropicClient",
    "_format_tool",
]


class AnthropicClient(LLMClient):
    """Anthropic Messages API 协议实现。"""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._session = Session(AnthropicAdapter(config))

    async def stream_chat(
        self,
        messages: list[APIMessage],
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        system_blocks: Any = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        async for event in self._session.stream_chat(
            messages,
            system_prompt=system_prompt,
            system_blocks=system_blocks,
            tools=tools,
        ):
            yield event
