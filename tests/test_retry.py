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


# ── 限流退避：Retry-After 优先、429 用独立基数 ─────────────────────────────
#
# 背景：0.5s 起步的通用退避只适用于 5xx/网络抖动。RPM/TPM 型限流按"分钟窗口"
# 恢复（sensenova 免费档实测 ~1 次/分钟，且只回 "rpm exhausted"、不给
# Retry-After），用通用退避必然在窗口打开前就把重试次数耗光，用户看到的是
# "AI 服务不可用"而不是"稍等重试"。


def _collect_sleeps(monkeypatch):
    """替换 asyncio.sleep，记录每次退避时长。"""
    import llm.session as session_mod

    sleeps: list[float] = []

    async def _fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(session_mod.asyncio, "sleep", _fake_sleep)
    return sleeps


def _mock_post_with_retry_after(monkeypatch, first: RawResponse):
    """第一次返回给定 RawResponse，第二次返回 200。"""
    import llm.session as session_mod

    calls = []

    async def _post_stream(url, headers, body, *, timeout):
        calls.append(1)
        if len(calls) == 1:
            yield first
            return
        yield RawResponse(200)
        yield 'data: {"ok":true}'

    monkeypatch.setattr(session_mod, "post_stream", _post_stream)
    return calls


async def test_retry_after_header_is_honored(monkeypatch):
    """上游给了 Retry-After 就照办，不用自算退避。"""
    sleeps = _collect_sleeps(monkeypatch)
    calls = _mock_post_with_retry_after(
        monkeypatch,
        RawResponse(429, error_message="rpm exhausted", retry_after=7.5),
    )

    s = Session(_Adapter(), max_retries=2, retry_delay=0.01, rate_limit_delay=99.0)
    out = await _collect(s)

    assert len(calls) == 2
    assert sleeps == [7.5], "应采信 Retry-After 而不是 rate_limit_delay"
    assert not any(isinstance(e, StreamError) for e in out)


async def test_429_without_retry_after_uses_rate_limit_delay(monkeypatch):
    """上游不给 Retry-After 时，429 用更长的独立基数（而非 retry_delay）。"""
    sleeps = _collect_sleeps(monkeypatch)
    _mock_post_with_retry_after(
        monkeypatch,
        RawResponse(429, error_message="rpm exhausted"),
    )

    s = Session(_Adapter(), max_retries=2, retry_delay=0.01, rate_limit_delay=4.0)
    await _collect(s)

    assert sleeps == [4.0], "429 应走 rate_limit_delay 基数"


async def test_5xx_still_uses_short_retry_delay(monkeypatch):
    """5xx 属瞬时抖动，仍走短的 retry_delay，不被限流基数拖长。"""
    sleeps = _collect_sleeps(monkeypatch)
    _mock_post_with_retry_after(monkeypatch, RawResponse(503, error_message="boom"))

    s = Session(_Adapter(), max_retries=2, retry_delay=0.25, rate_limit_delay=99.0)
    await _collect(s)

    assert sleeps == [0.25], "5xx 不该用限流基数"


async def test_retry_delay_grows_exponentially(monkeypatch):
    """连续限流时退避指数增长（给分钟级窗口留出恢复空间）。"""
    sleeps = _collect_sleeps(monkeypatch)

    import llm.session as session_mod

    async def _post_always_429(url, headers, body, *, timeout):
        yield RawResponse(429, error_message="rpm exhausted")

    monkeypatch.setattr(session_mod, "post_stream", _post_always_429)

    s = Session(_Adapter(), max_retries=3, retry_delay=0.01, rate_limit_delay=2.0)
    out = await _collect(s)

    assert sleeps == [2.0, 4.0, 8.0]
    assert any(isinstance(e, StreamError) for e in out)
    assert out[-1].code == "retry_exhausted"


def test_env_vars_override_defaults(monkeypatch):
    """环境变量是受限上游的运维逃生口；显式传参仍优先。"""
    monkeypatch.setenv("CODEFORGE_LLM_MAX_RETRIES", "5")
    monkeypatch.setenv("CODEFORGE_LLM_RATE_LIMIT_DELAY", "12.5")
    monkeypatch.setenv("CODEFORGE_LLM_RETRY_DELAY", "1.5")

    s = Session(_Adapter())
    assert s.max_retries == 5
    assert s.rate_limit_delay == 12.5
    assert s.retry_delay == 1.5

    explicit = Session(_Adapter(), max_retries=1)
    assert explicit.max_retries == 1, "显式传参优先于环境变量"


def test_env_vars_ignore_garbage(monkeypatch):
    """环境变量写坏不该让会话崩掉，退回默认值。"""
    monkeypatch.setenv("CODEFORGE_LLM_MAX_RETRIES", "not-a-number")

    s = Session(_Adapter())
    assert s.max_retries == 2


def test_parse_retry_after():
    """Retry-After 只认秒数形式；HTTP-date 与非法值退回 None。"""
    from llm.transport import _parse_retry_after

    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after(" 0.5 ") == 0.5
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not-a-number") is None
    assert _parse_retry_after("-1") is None
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
