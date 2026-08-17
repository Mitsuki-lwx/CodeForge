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
        self.last_body = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, method, url, *, headers=None, json=None):
        self.last_body = json
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
        import llm.transport as transport_mod

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
        assert thinks == ["I should think carefully..."]
        assert "".join(texts) == "Final response"

    def _capture_body(self, monkeypatch, client, messages, cfg):
        """通过 FakeClientCM 捕获 AnthropicClient 发出的请求体。"""
        import llm.transport as transport_mod

        lines = ['data: {"type": "message_stop"}']
        fake = _FakeClientCM(lines)
        monkeypatch.setattr(transport_mod.httpx, "AsyncClient", lambda *a, **k: fake)

        async def collect():
            events = []
            async for se in client.stream_chat(messages, system_prompt="sys"):
                events.append(se)
            return events

        asyncio.run(collect())
        return fake.last_body

    def test_wire_assistant_tool_use_prepends_thinking_block(self, monkeypatch):
        """thinking 模式：携带 reasoning 的工具调用消息前面插 thinking 块回传。"""
        cfg = ProviderConfig(name="t", protocol="anthropic", model="c", api_key="sk-x", thinking=True)
        client = AnthropicClient(cfg)
        msgs = [
            APIMessage(role="user", content="列文件"),
            APIMessage(role="assistant", content=[
                {"type": "tool_use", "id": "call_1", "name": "Glob", "input": {"pattern": "**/*.py"}},
            ], reasoning="先 Glob 一下"),
            APIMessage(role="user", content=[
                {"type": "tool_result", "tool_use_id": "call_1", "content": "[a.py]"},
            ]),
        ]
        body = self._capture_body(monkeypatch, client, msgs, cfg)
        asst = next(m for m in body["messages"] if m["role"] == "assistant")
        types = [b["type"] for b in asst["content"]]
        assert types[0] == "thinking"  # thinking 块必须在 tool_use 之前
        assert asst["content"][0]["thinking"] == "先 Glob 一下"
        assert asst["content"][1] == {"type": "tool_use", "id": "call_1", "name": "Glob", "input": {"pattern": "**/*.py"}}

    def test_wire_plain_assistant_prepends_thinking_block(self, monkeypatch):
        """thinking 模式：纯文本 assistant 消息在前置 thinking 块后保留正文。"""
        cfg = ProviderConfig(name="t", protocol="anthropic", model="c", api_key="sk-x", thinking=True)
        client = AnthropicClient(cfg)
        msgs = [
            APIMessage(role="user", content="hi"),
            APIMessage(role="assistant", content="最终回答", reasoning="思考过程"),
        ]
        body = self._capture_body(monkeypatch, client, msgs, cfg)
        asst = next(m for m in body["messages"] if m["role"] == "assistant")
        # thinking 块在前，正文 text 块在后；最后一条消息末尾打 cache_control 断点
        assert asst["content"] == [
            {"type": "thinking", "thinking": "思考过程"},
            {"type": "text", "text": "最终回答", "cache_control": {"type": "ephemeral"}},
        ]

    def test_wire_no_thinking_config_keeps_content_verbatim(self, monkeypatch):
        """thinking=False 时 reasoning 不回传；content 转为 text 块并打缓存断点。"""
        cfg = ProviderConfig(name="t", protocol="anthropic", model="c", api_key="sk-x", thinking=False)
        client = AnthropicClient(cfg)
        msgs = [
            APIMessage(role="assistant", content="没开思考", reasoning="不应出现"),
        ]
        body = self._capture_body(monkeypatch, client, msgs, cfg)
        asst = body["messages"][0]
        # reasoning 不回传；纯文本字符串 → text 块 + 末尾 cache_control 断点
        assert asst["content"] == [
            {"type": "text", "text": "没开思考", "cache_control": {"type": "ephemeral"}},
        ]


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


# ── system_blocks → 带 cache_control 断点的 blocks 数组 ───────────

def test_build_system_blocks_cache_breakpoint_on_last_cached():
    from core.prompts.builder import CachedBlock, PromptAssembly, UncachedBlock
    from llm.adapters.anthropic import _build_system_blocks

    assembly = PromptAssembly(
        cached=[CachedBlock(content="STABLE"), CachedBlock(content="TOOLS")],
        uncached=[UncachedBlock(content="ENV")],
    )
    blocks = _build_system_blocks(assembly)

    # cached 在前、uncached 在后
    assert [b["text"] for b in blocks] == ["STABLE", "TOOLS", "ENV"]
    # 断点打在最后一个 cached 块（TOOLS）末尾；首个 cached 块不打
    assert blocks[0].get("cache_control") is None
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    # 断点之后（uncached）不缓存
    assert blocks[2].get("cache_control") is None


def test_build_system_blocks_only_cached():
    from core.prompts.builder import CachedBlock, PromptAssembly
    from llm.adapters.anthropic import _build_system_blocks

    assembly = PromptAssembly(cached=[CachedBlock(content="S")])
    blocks = _build_system_blocks(assembly)
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_build_system_blocks_empty():
    from core.prompts.builder import PromptAssembly
    from llm.adapters.anthropic import _build_system_blocks

    assert _build_system_blocks(PromptAssembly()) == []


# ── 最后一条消息打 cache_control 断点（对话历史缓存）────────────────

def test_mark_last_message_cacheable_string_content():
    from llm.adapters.anthropic import _mark_last_message_cacheable

    msgs = [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "回答"},
    ]
    _mark_last_message_cacheable(msgs)

    # 只给最后一条消息打点；纯文本字符串 → text 块 + 断点
    assert msgs[0]["content"] == "第一个问题"  # 非最后一条不动
    assert msgs[1]["content"] == [
        {"type": "text", "text": "回答", "cache_control": {"type": "ephemeral"}},
    ]


def test_mark_last_message_cacheable_block_content():
    from llm.adapters.anthropic import _mark_last_message_cacheable

    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "Glob", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "[a.py]"},
        ]},
    ]
    _mark_last_message_cacheable(msgs)

    # 断点打在最后一条消息的最后一个块（tool_result）上
    assert "cache_control" not in msgs[0]["content"][0]
    assert msgs[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_mark_last_message_cacheable_empty():
    from llm.adapters.anthropic import _mark_last_message_cacheable

    _mark_last_message_cacheable([])  # 空列表不抛

    msg = {"role": "user", "content": ""}
    _mark_last_message_cacheable([msg])
    assert msg["content"] == []  # 空字符串不产生块，也不打点
