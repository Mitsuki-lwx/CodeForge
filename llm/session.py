"""Session：把 transport + adapter 组合成 stream_chat 行为。

对每个请求：
  1. adapter.build_request(...) 产出请求体；adapter.build_url/build_headers 产出端点。
  2. transport.post_stream(...) 发出流式请求，首项为 RawResponse，其后为原始 SSE 行。
  3. 非 200 → 统一：超长（PTL）抛 PromptTooLongError，否则 yield StreamError。
  4. 200 → adapter.emit_events(...) 把原始行解析成统一 StreamEvent 流，原样转发。

可靠性（spec：LLM 请求重试/降级）：
  - 暂时性失败（HTTP 429/5xx、网络错误/超时）自动重试，指数退避。
  - 仅在「响应未开始」（首项 RawResponse 之前）重试；200 后流进行中不重试（防重复计费）。
  - 重试耗尽 → yield StreamError(code="retry_exhausted")；PTL / 4xx 不重试。

对外暴露 `stream_chat(...)` 生成器 —— 契约与旧 `LLMClient.stream_chat` 完全一致，
因此客户端薄委托即可替换，调用方零改动。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from conversation.message import APIMessage
from llm import PromptTooLongError
from llm.stream_events import StreamError, StreamEvent
from llm.transport import RawResponse, post_stream

logger = logging.getLogger(__name__)

# 暂时性 HTTP 状态码：可重试（限流 / 服务端错误）
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class _Retryable(Exception):
    """标记一次可重试的暂时性失败（在响应开始前抛出）。

    retry_after：上游 `Retry-After` 给出的等待秒数（若有）。限流场景下它比任何
    自算退避都准，优先采信。
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


class Session:
    """一次对话请求的编排器，含暂时性失败重试。"""

    def __init__(
        self,
        adapter: Any,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_delay: float | None = None,
        rate_limit_delay: float | None = None,
    ) -> None:
        """参数为 None 时依次取环境变量、内置默认。

        `_env_*` 是为「上游限流很紧」这类环境准备的运维逃生口——受限上游（如
        免费档 API）往往 RPM 只有个位数，硬编码退避无法适配，且不该为它改代码：
            CODEFORGE_LLM_MAX_RETRIES / CODEFORGE_LLM_RETRY_DELAY /
            CODEFORGE_LLM_RATE_LIMIT_DELAY / CODEFORGE_LLM_TIMEOUT
        """
        self.adapter = adapter
        self.timeout = (
            timeout if timeout is not None
            else _env_float("CODEFORGE_LLM_TIMEOUT", 120.0)
        )
        self.max_retries = (
            max_retries if max_retries is not None
            else _env_int("CODEFORGE_LLM_MAX_RETRIES", 2)
        )
        self.retry_delay = (
            retry_delay if retry_delay is not None
            else _env_float("CODEFORGE_LLM_RETRY_DELAY", 0.5)
        )
        # 限流（429）单独用更长的退避基数：RPM/TPM 型限流按"分钟窗口"恢复，
        # 0.5s 起步的通用退避必然在窗口打开前就耗尽重试次数。
        self.rate_limit_delay = (
            rate_limit_delay if rate_limit_delay is not None
            else _env_float("CODEFORGE_LLM_RATE_LIMIT_DELAY", 5.0)
        )

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

        for attempt in range(self.max_retries + 1):
            try:
                async for event in self._attempt(url, headers, body):
                    yield event
                return
            except _Retryable as e:
                if attempt >= self.max_retries:
                    yield StreamError(
                        message=f"请求失败（重试 {self.max_retries} 次）：{e}",
                        code="retry_exhausted",
                    )
                    return
                # 上游给了 Retry-After 就照办（它最准）；否则按错误性质选基数：
                # 429 用 rate_limit_delay，其余（5xx/网络）用 retry_delay。
                if e.retry_after is not None:
                    delay = e.retry_after
                elif "429" in str(e) or "rate" in str(e).lower():
                    delay = self.rate_limit_delay * (2**attempt)
                else:
                    delay = self.retry_delay * (2**attempt)
                logger.warning(
                    "LLM 请求暂时失败（%s），%.1fs 后重试 %d/%d",
                    e,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(delay)

    async def _attempt(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        """单次请求尝试。响应开始前的暂时性失败抛 _Retryable（由外层重试）。"""
        stream = post_stream(url, headers, body, timeout=self.timeout)

        try:
            head = await stream.__anext__()
        except StopAsyncIteration:
            return
        except httpx.HTTPError as e:
            raise _Retryable(f"网络错误: {type(e).__name__}: {e}") from e

        if not isinstance(head, RawResponse):
            return

        if head.status_code != 200:
            if self.adapter.is_prompt_too_long(head.error_message, head.error_code):
                raise PromptTooLongError(head.error_message)
            if head.status_code in _RETRYABLE_STATUS:
                raise _Retryable(
                    f"HTTP {head.status_code}: {head.error_message}",
                    retry_after=head.retry_after,
                )
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
