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
        """自定义 base_url 生效（经 Session→Adapter 拼接请求端点）。"""
        cfg = ProviderConfig(
            name="t", protocol="openai", model="gpt-4", api_key="k",
            base_url="https://custom.example.com/v1",
        )
        client = OpenAIClient(cfg)
        assert "custom.example.com" in client._session.adapter.build_url(
            client._session.adapter.build_base_url()
        )

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
        import llm.transport as transport_mod

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
        monkeypatch.setattr(transport_mod.httpx, "AsyncClient", lambda *a, **k: _FakeClientCM(lines))

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


# ── 出站 wire format：Anthropic 内容块 → OpenAI tool_calls / role="tool" ──


def _wire(m):
    """返回 list[dict]，单个内部消息可能展开成多条 wire 消息。"""
    from llm.openai_client import _to_openai_wire
    return _to_openai_wire(m)


def test_wire_text_message():
    """纯文本消息 → 内容块数组。"""
    from conversation.message import APIMessage
    out = _wire(APIMessage(role="assistant", content="hello"))
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == [{"type": "text", "text": "hello"}]


def test_wire_user_text_message():
    """user 纯文本也转成块数组（避免 endpoint 期待 block 却见裸字符串）。"""
    from conversation.message import APIMessage
    out = _wire(APIMessage(role="user", content="hi"))
    assert out[0]["content"] == [{"type": "text", "text": "hi"}]


def test_wire_assistant_tool_use():
    """assistant 的 tool_use 块 → 顶级 tool_calls, arguments 为 JSON 字符串。"""
    from conversation.message import APIMessage
    msg = APIMessage(role="assistant", content=[
        {"type": "text", "text": "先查一下"},
        {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "/a.txt"}},
    ])
    out = _wire(msg)
    assert len(out) == 1
    e = out[0]
    assert e["role"] == "assistant"
    assert e["content"] == [{"type": "text", "text": "先查一下"}]
    assert e["tool_calls"] == [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "/a.txt"}'},
    }]


def test_wire_assistant_only_tool_use_empty_content():
    """只有 tool_use、无文本时 content 为空数组。"""
    from conversation.message import APIMessage
    msg = APIMessage(role="assistant", content=[
        {"type": "tool_use", "id": "c", "name": "grep_tool", "input": {"q": "x"}},
    ])
    out = _wire(msg)
    e = out[0]
    assert e["role"] == "assistant"
    assert e["content"] == []
    assert e["tool_calls"][0]["function"]["name"] == "grep_tool"


def test_wire_tool_result_becomes_tool_role():
    """user 的 tool_result 块 → role="tool" + tool_call_id。"""
    from conversation.message import APIMessage
    msg = APIMessage(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_1", "content": "文件内容……"},
    ])
    out = _wire(msg)
    assert out == [{"role": "tool", "tool_call_id": "call_1", "content": "文件内容……"}]


def test_wire_multiple_tool_results_expand_each_as_tool():
    """一个含多个 tool_result 块的消息 → 每块一条 role='tool' 消息（多工具调用时必需）。"""
    from conversation.message import APIMessage
    msg = APIMessage(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_00", "content": "A目录列表"},
        {"type": "tool_result", "tool_use_id": "call_01", "content": "B文件列表"},
    ])
    out = _wire(msg)
    assert out == [
        {"role": "tool", "tool_call_id": "call_00", "content": "A目录列表"},
        {"role": "tool", "tool_call_id": "call_01", "content": "B文件列表"},
    ]


def test_wire_bytes_tool_result():
    """tool_result 内容为 bytes 时解码为文本。"""
    from conversation.message import APIMessage
    msg = APIMessage(role="user", content=[
        {"type": "tool_result", "tool_use_id": "call_9", "content": b"raw\xff"},
    ])
    out = _wire(msg)
    assert out[0]["content"] == "raw�"


def test_wire_plain_assistant_reasoning_top_level():
    """assistant 文本消息的 reasoning → 顶层 reasoning_content。"""
    from conversation.message import APIMessage
    msg = APIMessage(role="assistant", content="查一下结构", reasoning="先看目录再分析")
    out = _wire(msg)
    e = out[0]
    assert e["role"] == "assistant"
    assert e["content"] == [{"type": "text", "text": "查一下结构"}]
    assert e["reasoning_content"] == "先看目录再分析"


def test_wire_tool_use_message_carries_reasoning():
    """assistant 工具调用消息必须带上 reasoning_content（DeepSeek thinking 模式必需）。"""
    from conversation.message import APIMessage
    msg = APIMessage(
        role="assistant",
        content=[
            {"type": "tool_use", "id": "call_1", "name": "glob_tool", "input": {"pattern": "**/*.py"}},
        ],
        reasoning="我先 Glob 一下文件列表",
    )
    out = _wire(msg)
    e = out[0]
    assert e["reasoning_content"] == "我先 Glob 一下文件列表"
    assert e["tool_calls"][0]["function"]["name"] == "glob_tool"


def test_wire_tool_result_never_receives_reasoning_field():
    """tool result 消息不应带上 assistant 的 reasoning。"""
    from conversation.message import APIMessage
    msg = APIMessage(
        role="user",
        content=[{"type": "tool_result", "tool_use_id": "call_1", "content": "xx"}],
        reasoning="不应出现",
    )
    out = _wire(msg)
    assert out[0]["role"] == "tool"
    assert "reasoning_content" not in out[0]
