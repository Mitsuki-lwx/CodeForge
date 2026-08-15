"""Session：把 transport + adapter 组合成 stream_chat 行为。

对每个请求：
  1. adapter.build_request(...) 产出请求体；adapter.build_url/build_headers 产出端点。
  2. transport.post_stream(...) 发出流式请求，首项为 RawResponse，其后为原始 SSE 行。
  3. 非 200 → 统一：超长（PTL）抛 PromptTooLongError，否则 yield StreamError。
  4. 200 → adapter.emit_events(...) 把原始行解析成统一 StreamEvent 流，原样转发。

对外暴露 `stream_chat(...)` 生成器 —— 契约与旧 `LLMClient.stream_chat` 完全一致，
因此客户端薄委托即可替换，调用方零改动。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from conversation.message import APIMessage
from llm import PromptTooLongError
from llm.stream_events import StreamError, StreamEvent
from llm.transport import RawResponse, post_stream


class Session:
    """一次对话请求的编排器。"""

    def __init__(self, adapter: Any, *, timeout: float = 120.0) -> None:
        self.adapter = adapter
        self.timeout = timeout

    async def stream_chat(
        self,
        messages: list[APIMessage],
        *,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        system_blocks: Any = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        url = self.adapter.build_url(self.adapter.build_base_url())
        headers = self.adapter.build_headers()
        body = await self.adapter.build_request(
            messages,
            system_prompt=system_prompt,
            system_blocks=system_blocks,
            tools=tools,
        )

        stream = post_stream(url, headers, body, timeout=self.timeout)

        # 首项必须是 RawResponse（头部：状态/错误）
        try:
            head = await stream.__anext__()
        except StopAsyncIteration:
            return
        if not isinstance(head, RawResponse):
            return

        if head.status_code != 200:
            if self.adapter.is_prompt_too_long(head.error_message, head.error_code):
                raise PromptTooLongError(head.error_message)
            yield StreamError(
                message=head.error_message,
                code=str(head.status_code),
            )
            return

        # 200：把剩余的原始行流交给 adapter 解析
        # gen_ai.* OTel span：让 Langfuse 等把这次请求识别为 LLM 调用（覆盖所有调用方）。
        try:
            from llm.llm_span import gen_ai_span, wrap_events

            span_ctx = gen_ai_span(
                protocol=getattr(self.adapter.config, "protocol", ""),
                model=getattr(self.adapter.config, "model", ""),
                provider=getattr(self.adapter.config, "name", ""),
            )
        except Exception:  # noqa: BLE001 —— 观测失败绝不改变语义
            span_ctx = None

        events = self.adapter.emit_events(stream)
        if span_ctx is not None:
            events = wrap_events(events, span_ctx)
        async for event in events:
            yield event
