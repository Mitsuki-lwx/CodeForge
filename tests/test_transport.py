"""传输层单测：post_stream 的中途关闭竞态。"""

from __future__ import annotations

import asyncio

import pytest

import llm.transport as transport
from llm.transport import RawResponse, post_stream


# ── 模拟 httpx 流式响应：aclose 在 aiter_lines 未关闭时抛 "already running" ──


class _FakeResponse:
    """aiter_lines 是异步生成器；aclose 若它仍挂在迭代上则抛 RuntimeError。"""

    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._iter_closed = False

    async def aiter_lines(self):
        try:
            for ln in self._lines:
                yield ln
        finally:
            self._iter_closed = True

    async def aclose(self) -> None:
        # 模拟 httpx：底层流仍在迭代（aiter_lines 未关闭）→ already running
        if not self._iter_closed:
            raise RuntimeError("aclose(): asynchronous generator is already running")

    async def aread(self) -> bytes:
        return b""


class _FakeStreamCM:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc) -> bool:
        await self._response.aclose()  # 模拟 async with 退出时的 response.aclose()
        return False


class _FakeClientCM:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, *a, **kw):
        return _FakeStreamCM(self._response)


@pytest.mark.asyncio
async def test_post_stream_close_mid_iteration_no_race(monkeypatch):
    """中途关闭 post_stream 不应触发 aclose 竞态（RuntimeError）。"""
    resp = _FakeResponse(["data: a", "data: b", "data: c"])
    monkeypatch.setattr(
        transport.httpx, "AsyncClient", lambda *a, **k: _FakeClientCM(resp)
    )

    gen = post_stream("http://x", {}, {})
    head = await gen.__anext__()
    assert isinstance(head, RawResponse)
    assert await gen.__anext__() == "data: a"  # 拿第一行后中途关闭
    await gen.aclose()  # 不抛即通过（修复前会 RuntimeError）


@pytest.mark.asyncio
async def test_post_stream_consumes_fully(monkeypatch):
    """正常消费完再关闭，不抛错。"""
    resp = _FakeResponse(["data: a", "data: b"])
    monkeypatch.setattr(
        transport.httpx, "AsyncClient", lambda *a, **k: _FakeClientCM(resp)
    )

    gen = post_stream("http://x", {}, {})
    events = []
    async for ev in gen:
        events.append(ev)
    assert isinstance(events[0], RawResponse)
    assert events[1:] == ["data: a", "data: b"]
