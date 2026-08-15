"""Adapter 抽象：wire 封装 + 响应解析。

一个 Adapter 描述某个协议/厂商如何：
  - build_url(base_url)：拼出该协议的请求端点。
  - build_headers()：构造请求头（含 Authorization）。
  - build_request(messages, *, system_prompt, system_blocks, tools) → dict：
    把内部 `APIMessage` 列表封装成该协议的请求体。
  - emit_events(lines) → AsyncGenerator[StreamEvent]：
    把 transport 的原始 SSE 行解析成统一 `StreamEvent` 流（负责 PromptTooLongError、
    StreamError、CompletionDone、ToolUse、ThinkingChunk 的全部分发）。

session 层负责把 transport + adapter 组合成 stream_chat 行为。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from conversation.message import APIMessage
from llm.stream_events import StreamEvent


class Adapter:
    """协议/厂商适配器的抽象基类。"""

    # 该协议把非 200 错误识别为上下文过长（触发紧急压缩）的子串匹配
    ptl_markers: tuple[str, ...] = ()
    # 相对 base_url 的端点路径
    endpoint_path = ""

    def __init__(self, config: Any) -> None:
        self.config = config

    def is_prompt_too_long(self, message: str, code: str) -> bool:
        """判断错误是否属于上下文过长，需要抛出 PromptTooLongError。"""
        if code == "context_length_exceeded":
            return True
        lower = message.lower()
        return any(m in lower for m in self.ptl_markers)

    def build_url(self, base_url: str) -> str:
        return base_url.rstrip("/") + self.endpoint_path

    def build_headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def build_request(
        self,
        messages: list[APIMessage],
        *,
        system_prompt: str,
        system_blocks: Any,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def emit_events(
        self, lines: AsyncGenerator[str, None]
    ) -> AsyncGenerator[StreamEvent, None]:
        raise NotImplementedError
