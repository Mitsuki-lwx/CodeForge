"""统一 HTTP 流式传输层。

职责：只管把一个 HTTP POST（含 body）发出去，把响应暴露为异步事件流。
**不含**任何消息语义（wire 封装/解析由 adapters 承担，编排由 session 承担）。

`post_stream(...)` 是一个异步生成器，产出两种 item：
  1. 第一项恒为 `RawResponse`（头部，含 status_code / error_message / error_code）。
  2. 200 之后逐行 yield 原始 SSE 行字符串（含 `data: ` 前缀）。

客户端 context（httpx.AsyncClient）在整个迭代期间保持打开，因此调用方必须
在会话结束前完整消费或析构本生成器 —— 与原先两个客户端在自身 stream_chat 内
持有 context 的行为一致。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any

import httpx

HEAD = "head"


class RawResponse:
    """流式请求的头部：状态与错误信息。"""

    def __init__(
        self,
        status_code: int,
        error_message: str = "",
        error_code: str = "",
    ) -> None:
        self.status_code = status_code
        self.error_message = error_message
        self.error_code = error_code


def _extract_error(body: bytes) -> tuple[str, str]:
    """从非 200 响应体提取 (错误消息, 错误码)。"""
    try:
        err_data = json.loads(body)
        msg = err_data.get("error", {}).get("message", str(body))
        code = err_data.get("error", {}).get("code", "")
        return str(msg), str(code)
    except (json.JSONDecodeError, AttributeError):
        return body.decode(errors="replace"), ""


async def post_stream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    timeout: float = 120.0,
) -> AsyncGenerator[RawResponse | str, None]:
    """发起一个流式 POST。

    Yields:
        首项 RawResponse（状态/错误）；200 后逐行 yield 原始 SSE 行字符串。
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                message, code = _extract_error(error_body)
                yield RawResponse(
                    response.status_code,
                    error_message=message,
                    error_code=code,
                )
                return
            yield RawResponse(response.status_code)
            # 显式持有内层 aiter_lines 生成器，而不是直接 `async for`：
            # 中途被关闭（GeneratorExit/取消）时，先在 finally 里关掉它，
            # 级联关闭底层 httpx 流，避免 async with 退出时 response.aclose()
            # 撞上仍挂在迭代上的内层生成器（RuntimeError: already running）。
            lines = response.aiter_lines()
            try:
                async for line in lines:
                    yield line
            finally:
                with suppress(RuntimeError):
                    await lines.aclose()
