"""OpenAI 适配器 + Session + transport 集成：验证多工具展开与 vendor 协商。

通过真实 OpenAIClient → Session → transport，但注入假 httpx 捕获请求体，
断言 outbound body 的各项深度规则（多 tool_result → 每调一条 role=tool 等）。
"""

from __future__ import annotations

import asyncio

import pytest

import llm.transport as transport_mod
from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.openai_client import OpenAIClient
from llm.protocol import resolve_adapter_class


class _R:
    status_code = 200

    async def aiter_lines(self):
        yield 'data: [DONE]'

    async def aread(self):
        return b""


class _SCM:
    async def __aenter__(self):
        return _R()

    async def __aexit__(self, *e):
        return False


class _FakeClient:
    def __init__(self):
        self.last_body = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    def stream(self, method, url, *, headers=None, json=None):
        self.last_body = json
        return _SCM()


@pytest.fixture
def capture(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(transport_mod.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


def test_multi_tool_result_expands_to_separate_tool_messages(monkeypatch, capture):
    """一个含多 tool_result 的消息展开成每 tool_call_id 一条 role=tool。"""
    cfg = ProviderConfig(name="t", protocol="openai", model="gpt-4", api_key="k", thinking=False)
    client = OpenAIClient(cfg)
    msgs = [
        APIMessage(role="user", content="列文件"),
        APIMessage(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "glob", "input": {"pattern": "a"}},
            {"type": "tool_use", "id": "call_1", "name": "bash", "input": {"cmd": "ls"}},
        ]),
        APIMessage(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_0", "content": "resA"},
            {"type": "tool_result", "tool_use_id": "call_1", "content": "resB"},
        ]),
    ]

    async def collect():
        async for _ in client.stream_chat(msgs, system_prompt="sys"):
            pass

    asyncio.run(collect())
    body = capture.last_body
    tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "call_0"
    assert tool_msgs[0]["content"] == "resA"
    assert tool_msgs[1]["tool_call_id"] == "call_1"
    assert tool_msgs[1]["content"] == "resB"
    # content 恒为块数组，不出现裸字符串
    for m in body["messages"]:
        if m["role"] in ("assistant", "user"):
            assert isinstance(m["content"], list)


def test_reasoning_carried_on_tool_call_assistant(monkeypatch, capture):
    """thinking 回传：assistant 工具调用消息带顶层 reasoning_content。"""
    cfg = ProviderConfig(name="t", protocol="openai", model="gpt-4", api_key="k", thinking=True)
    client = OpenAIClient(cfg)
    msgs = [
        APIMessage(role="assistant", content=[
            {"type": "tool_use", "id": "call_1", "name": "glob", "input": {"pattern": "*.py"}},
        ], reasoning="需要先查文件"),
    ]

    async def collect():
        async for _ in client.stream_chat(msgs, system_prompt="sys"):
            pass

    asyncio.run(collect())
    body = capture.last_body
    asst = next(m for m in body["messages"] if m["role"] == "assistant")
    assert asst["reasoning_content"] == "需要先查文件"
    assert asst["tool_calls"][0]["function"]["name"] == "glob"


def test_vendor_deepseek_resolves_deepseek_adapter():
    """显式 vendor=deepseek → OpenAIClient 内部用 DeepSeek 适配器。"""
    cfg = ProviderConfig(name="t", protocol="openai", model="deepseek-v4-flash",
                         api_key="k", base_url="https://api.deepseek.com", vendor="deepseek")
    client = OpenAIClient(cfg)
    assert type(client._session.adapter).__name__ == "DeepSeekConversationAdapter"


def test_vendor_auto_detect_plain_openai():
    """无 vendor + 普通端点 → 基础 OpenAI 适配器。"""
    assert resolve_adapter_class(
        "openai", None, "gpt-4o", "https://api.openai.com/v1"
    ).__name__ == "OpenAIConversationAdapter"
