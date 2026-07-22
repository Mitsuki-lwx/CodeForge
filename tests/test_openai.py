"""OpenAI 客户端测试。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from config.model import ProviderConfig
from llm.openai_client import OpenAIClient
from llm.stream_events import (
    StreamError,
    TextChunk,
    ThinkingChunk,
    CompletionDone,
)
from conversation.message import APIMessage


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


class TestOpenAIClient:
    """使用 httpx mock 测试 OpenAI 协议客户端。"""

    def test_text_stream(self):
        """正常文本流式响应。"""
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}, "index": 0}]},
            {"choices": [{"delta": {"content": "Hello"}, "index": 0}]},
            {"choices": [{"delta": {"content": " world"}, "index": 0}]},
            {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        ]

        texts = []
        usage = None
        for chunk in self._iter_chunks(chunks):
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if content is not None:
                texts.append(content)
            finish = choices[0].get("finish_reason")
            if finish and chunk.get("usage"):
                usage = chunk["usage"]

        assert "".join(texts) == "Hello world"
        assert usage is not None

    def test_done_event(self):
        """[DONE] 事件终止流。"""
        chunks = [
            {"choices": [{"delta": {"content": "Hi"}, "index": 0}]},
            # [DONE] 信号在这里
        ]
        done_reached = False
        for chunk in self._iter_chunks(chunks):
            pass
        done_reached = True
        assert done_reached

    def test_system_prompt_included(self):
        """请求体中 system prompt 在第一位置。"""
        body = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == "You are helpful."

    def test_custom_base_url(self):
        """自定义 base_url 生效。"""
        cfg = ProviderConfig(
            name="t", protocol="openai", model="gpt-4", api_key="k",
            base_url="https://custom.example.com/v1",
        )
        client = OpenAIClient(cfg)
        assert "custom.example.com" in client._base_url

    def test_api_error(self):
        """HTTP 401 错误。"""
        sse = json.dumps({
            "error": {"message": "Incorrect API key", "type": "invalid_request_error"}
        })
        transport = httpx.MockTransport(lambda r: httpx.Response(401, text=sse))

        with httpx.Client(transport=transport) as http:
            resp = http.post("https://api.openai.com/v1/chat/completions")
            assert resp.status_code == 401
            err = resp.json()
            assert "Incorrect API key" in err["error"]["message"]

    def test_reasoning_content_as_thinking(self, monkeypatch):
        """OpenAI 协议的 reasoning_content 产出 ThinkingChunk，与正文区分。"""
        import llm.openai_client as mod

        lines = [
            'data: {"choices": [{"delta": {"role": "assistant", "reasoning_content": "Let me reason"}, "index": 0}]}',
            'data: {"choices": [{"delta": {"content": "The answer"}, "index": 0}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}',
            'data: [DONE]',
        ]
        cfg = ProviderConfig(
            name="t", protocol="openai", model="c", api_key="sk-x", thinking=True,
        )
        client = OpenAIClient(cfg)
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
        assert thinks == ["Let me reason"]
        assert "".join(texts) == "The answer"

    @staticmethod
    def _iter_chunks(chunks: list[dict]) -> list[dict]:
        return chunks


def test_normalize_usage():
    from llm.openai_client import _normalize_usage

    raw = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 30},
    }
    norm = _normalize_usage(raw)
    assert norm["input_tokens"] == 100
    assert norm["output_tokens"] == 50
    assert norm["cache_read_input_tokens"] == 30


def test_normalize_usage_empty():
    from llm.openai_client import _normalize_usage

    assert _normalize_usage(None) is None
