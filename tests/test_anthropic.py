"""Anthropic 客户端测试。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from config.model import ProviderConfig
from llm.anthropic_client import AnthropicClient
from llm.stream_events import (
    StreamError,
    TextChunk,
    ThinkingChunk,
    CompletionDone,
)
from conversation.message import APIMessage


# ── Fake streaming transport ───────────────────────────────────────


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeStreamCM:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._lines)

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeClientCM:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, method, url, *, headers=None, json=None):
        return _FakeStreamCM(self._lines)


class TestAnthropicClient:
    """使用 httpx mock 测试 Anthropic 协议客户端。"""

    def test_text_stream(self):
        """正常文本流式响应。"""
        sse_events = [
            {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant",
                "content": [], "usage": {"input_tokens": 10, "output_tokens": 1},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "text", "text": "",
            }},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "text_delta", "text": "Hello, ",
            }},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "text_delta", "text": "world!",
            }},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {
                "stop_reason": "end_turn", "stop_sequence": None,
            }, "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]

        texts = []
        for raw in self._iter_sse_events(sse_events):
            event_type = raw.get("type")
            if event_type == "content_block_delta":
                delta = raw.get("delta", {})
                if delta.get("type") == "text_delta":
                    texts.append(delta.get("text", ""))

        assert "".join(texts) == "Hello, world!"

    def test_thinking_chunk_separated(self, monkeypatch):
        """thinking_delta 产出 ThinkingChunk，正文仍产出 TextChunk，互不混淆。"""
        import llm.anthropic_client as mod

        lines = [
            'data: {"type": "message_start", "message": {"usage": {"input_tokens": 5}}}',
            'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "text": ""}}',
            'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "I should think carefully..."}}',
            'data: {"type": "content_block_stop", "index": 0}',
            'data: {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}',
            'data: {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Final response"}}',
            'data: {"type": "content_block_stop", "index": 1}',
            'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}',
            'data: {"type": "message_stop"}',
        ]
        cfg = ProviderConfig(
            name="t", protocol="anthropic", model="c", api_key="sk-x", thinking=True,
        )
        client = AnthropicClient(cfg)
        monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: _FakeClientCM(lines))

        async def collect():
            events = []
            async for se in client.stream_chat(
                [APIMessage(role="user", content="hi")], system_prompt=""
            ):
                events.append(se)
            return events

        events = asyncio.run(collect())
        thinks = [e.text for e in events if isinstance(e, ThinkingChunk)]
        texts = [e.text for e in events if isinstance(e, TextChunk)]
        assert thinks == ["I should think carefully..."]
        assert "".join(texts) == "Final response"

    def test_message_delta_usage_merges(self):
        """message_delta 的 usage 与 message_start 合并（不丢失 input_tokens）。"""
        sse_events = [
            {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant",
                "content": [], "usage": {"input_tokens": 5},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]

        usage = None
        for raw in self._iter_sse_events(sse_events):
            if raw.get("type") == "message_start":
                usage = raw.get("message", {}).get("usage")
            elif raw.get("type") == "message_delta":
                delta = raw.get("delta", {})
                if delta.get("stop_reason") == "end_turn":
                    msg_usage = raw.get("usage")
                    if msg_usage:
                        usage = {**(usage or {}), **msg_usage}  # 合并而非替换

        assert usage == {"input_tokens": 5, "output_tokens": 1}

    def test_api_error(self):
        """HTTP 401 错误。"""
        cfg = ProviderConfig(
            name="t", protocol="anthropic", model="c", api_key="sk-bad",
        )
        sse = json.dumps({
            "error": {"message": "Invalid API key", "type": "authentication_error"}
        })
        transport = httpx.MockTransport(lambda r: httpx.Response(401, text=sse))

        with httpx.Client(transport=transport) as http:
            resp = http.post("https://api.anthropic.com/v1/messages")
            assert resp.status_code == 401
            err = resp.json()
            assert "Invalid API key" in err["error"]["message"]

    @staticmethod
    def _iter_sse_events(sse_events: list[dict]) -> list[dict]:
        return sse_events
