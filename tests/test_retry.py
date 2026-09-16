"""LLM 请求重试/降级（llm.session）单测。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from llm import PromptTooLongError
from llm.session import Session
from llm.stream_events import CompletionDone, StreamError, TextChunk
from llm.transport import RawResponse


class _Adapter:
    config = SimpleNamespace(protocol="anthropic", model="m", name="p")

    def build_url(self, base):
        return "http://x/v1/messages"

    def build_base_url(self):
        return "http://x"

    def build_headers(self):
        return {}

    async def build_request(self, *a, **k):
        return {}

    def is_prompt_too_long(self, msg, code):
        return "prompt is too long" in (msg or "")

    def emit_events(self, stream):
        async def _gen():
            async for _line in stream:
                yield TextChunk(text="ok")
            yield CompletionDone(usage={})

        return _gen()


async def _collect(session):
    out = []
    async for ev in session.stream_chat([], system_prompt="", tools=None):
        out.append(ev)
    return out


def _mock_post(monkeypatch, statuses):
    """mock post_stream：按调用次数依次返回 statuses 里的状态。"""
    import llm.session as session_mod

    calls = []

    async def _post_stream(url, headers, body, *, timeout):
        calls.append(1)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        if isinstance(status, Exception):
            raise status
        yield RawResponse(status)
        if status == 200:
            yield 'data: {"ok":true}'

    monkeypatch.setattr(session_mod, "post_stream", _post_stream)
    return calls


async def test_retry_5xx_then_success(monkeypatch):
    calls = _mock_post(monkeypatch, [503, 200])
    s = Session(_Adapter(), max_retries=2, retry_delay=0.01)
    out = await _collect(s)
    assert len(calls) == 2  # 重试后成功
    assert any(isinstance(e, TextChunk) for e in out)
    assert not any(isinstance(e, StreamError) for e in out)


async def test_retry_429_then_success(monkeypatch):
    calls = _mock_post(monkeypatch, [429, 200])
    s = Session(_Adapter(), max_retries=2, retry_delay=0.01)
    out = await _collect(s)
    assert len(calls) == 2
    assert any(isinstance(e, TextChunk) for e in out)


async def test_retry_network_error_then_success(monkeypatch):
    calls = _mock_post(monkeypatch, [httpx.ConnectError("boom"), 200])
    s = Session(_Adapter(), max_retries=2, retry_delay=0.01)
    out = await _collect(s)
    assert len(calls) == 2
    assert any(isinstance(e, TextChunk) for e in out)


async def test_retry_exhausted_yields_stream_error(monkeypatch):
    calls = _mock_post(monkeypatch, [503, 503, 503, 503])
    s = Session(_Adapter(), max_retries=2, retry_delay=0.01)
    out = await _collect(s)
    assert len(calls) == 3  # 初始 + 2 次重试
    err = [e for e in out if isinstance(e, StreamError)]
    assert err and err[0].code == "retry_exhausted"


async def test_4xx_not_retried(monkeypatch):
    calls = _mock_post(monkeypatch, [400])
    s = Session(_Adapter(), max_retries=2, retry_delay=0.01)
    out = await _collect(s)
    assert len(calls) == 1  # 4xx 不重试
    err = [e for e in out if isinstance(e, StreamError)]
    assert err and err[0].code == "400"


async def test_ptl_not_retried(monkeypatch):
    class _PTLAdapter(_Adapter):
        def is_prompt_too_long(self, msg, code):
            return True

    calls = _mock_post(monkeypatch, [400])
    s = Session(_PTLAdapter(), max_retries=2, retry_delay=0.01)
    try:
        await _collect(s)
        assert False, "应抛 PromptTooLongError"
    except PromptTooLongError:
        pass
    assert len(calls) == 1  # PTL 不重试
