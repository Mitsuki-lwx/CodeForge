"""OpenAI Chat Completions 协议客户端。

薄委托层：持有 .config，按其协商出的 OpenAI 系 Session 执行流式对话。
wire 封装/解析已迁至 llm.adapters（openai_base / openai_deepseek）。

`OpenAIClient` 名称、构造函数 (config)、`.config`、`stream_chat` 签名均保持，
调用方与既有按名单测零改动；`_to_openai_wire`/`_normalize_usage`/
`DEFAULT_OPENAI_BASE_URL` 从适配器 re-export 以兼容测试导入。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.adapters.openai_base import (
    DEFAULT_OPENAI_BASE_URL,
    _normalize_usage,
    _to_openai_wire,
)
from llm.client import LLMClient
from llm.protocol import resolve_adapter_class
from llm.session import Session
from llm.stream_events import StreamEvent

__all__ = [
    "DEFAULT_OPENAI_BASE_URL",
    "OpenAIClient",
    "_normalize_usage",
    "_to_openai_wire",
]


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions 协议实现（兼容 OpenAI / DeepSeek 等端点）。"""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        adapter_cls = resolve_adapter_class(
            config.protocol, config.vendor, config.model, config.base_url
        )
        self._session = Session(adapter_cls(config))

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
