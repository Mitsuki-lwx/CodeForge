"""Anthropic Messages 协议客户端。

薄委托层：持有 .config，按其协商出的 Anthropic Session 执行流式对话。
wire 封装/解析在 llm.adapters.anthropic。

适配器注册表按**字符串**引用本类（`client="llm.anthropic_client.AnthropicClient"`），
所以这个模块名与类名是协议的一部分，不能随意改。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.adapters.anthropic import AnthropicAdapter
from llm.client import LLMClient
from llm.session import Session
from llm.stream_events import StreamEvent

__all__ = ["AnthropicClient"]


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
