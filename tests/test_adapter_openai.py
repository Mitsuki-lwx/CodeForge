"""OpenAI 适配器 + Session + transport 集成：验证多工具展开与 vendor 协商。

通过真实 OpenAIClient → Session → transport，但注入假 httpx 捕获请求体，
断言 outbound body 的各项深度规则（多 tool_result → 每调一条 role=tool 等）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

import llm.transport as transport_mod
from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.openai_client import OpenAIClient
from llm.protocol import resolve_adapter_class
from llm.stream_events import CompletionDone, ToolUse


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


# ── finish_reason 兼容性：空字符串 vs null ────────────────────────────────
#
# 标准 OpenAI 用 `finish_reason: null` 表示"仍在流中"，但部分兼容上游
# （实测 SenseNova / token.sensenova.cn）把未结束填成空字符串 ""。
# 旧实现按 `is not None` 判定结束，导致累积期间每个 chunk 都发射一次
# 工具调用——实测一次 write_file 被发成 9 个 ToolUse，前几个 input 还是
# 空 {}，上层会据此重复执行工具。


def _sensenova_adapter():
    """构造走 sensenova 端点的 DeepSeek 适配器（vendor=deepseek 复用 reasoning 回传）。"""
    cfg = ProviderConfig(
        name="SenseNova",
        protocol="openai",
        vendor="deepseek",
        model="deepseek-v4-flash",
        api_key="k",
        base_url="https://token.sensenova.cn/v1",
    )
    cls = resolve_adapter_class("openai", cfg.vendor, cfg.model, cfg.base_url)
    return cls(cfg)


def _chunk(delta: dict, finish=None) -> str:
    """构造一条 OpenAI 风格 SSE 行。finish=None 序列化为 JSON null。"""
    return "data: " + json.dumps(
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


def _emit(rows: list[str]) -> list:
    """把 SSE 行喂给适配器，收集全部事件。"""
    adapter = _sensenova_adapter()

    async def gen():
        for r in rows:
            yield r

    async def collect():
        return [ev async for ev in adapter.emit_events(gen())]

    return asyncio.run(collect())


def _tools_arg(part: str, idx: int = 0, call_id: str = "", name: str = "") -> dict:
    return {
        "index": idx,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": part},
    }


def test_empty_finish_reason_emits_single_tool_use():
    """空字符串 finish_reason（sensenova 实况）不得重复发射工具调用。

    复刻真实抓包序列：4 个 chunk 带 finish_reason=""，只有最后一个是
    "tool_calls"。修复前会发出多个 ToolUse（含空 input）。
    """
    rows = [
        _chunk({"role": "assistant", "content": "", "tool_calls": [
            _tools_arg("", call_id="call_x", name="write_file")]}, finish=""),
        _chunk({"content": "", "tool_calls": [
            _tools_arg('{"path": "a.txt", ')]}, finish=""),
        _chunk({"content": "", "tool_calls": [
            _tools_arg('"content": "hi"}')]}, finish=""),
        _chunk({"content": ""}, finish="tool_calls"),
        "data: [DONE]",
    ]

    events = _emit(rows)
    tools = [e for e in events if isinstance(e, ToolUse)]

    assert len(tools) == 1, f"工具调用被重复发射 {len(tools)} 次: {tools}"
    assert tools[0].id == "call_x"
    assert tools[0].name == "write_file"
    assert tools[0].input == {"path": "a.txt", "content": "hi"}
    assert any(isinstance(e, CompletionDone) for e in events)


def test_repeated_nonempty_finish_reason_does_not_duplicate():
    """上游重复给出非空 finish_reason 时，工具调用仍只发射一次。"""
    rows = [
        _chunk({"tool_calls": [_tools_arg('{"p": 1}', call_id="call_y", name="glob")]},
               finish="tool_calls"),
        _chunk({}, finish="tool_calls"),
        "data: [DONE]",
    ]

    tools = [e for e in _emit(rows) if isinstance(e, ToolUse)]

    assert len(tools) == 1
    assert tools[0].input == {"p": 1}


def test_done_only_upstream_still_emits_pending_tools():
    """上游全程不给非空 finish_reason、只以 [DONE] 收尾时，工具调用不能丢。"""
    rows = [
        _chunk({"tool_calls": [_tools_arg('{"cmd": "ls"}', call_id="call_z", name="bash")]}),
        "data: [DONE]",
    ]

    events = _emit(rows)
    tools = [e for e in events if isinstance(e, ToolUse)]

    assert len(tools) == 1
    assert tools[0].name == "bash"
    assert tools[0].input == {"cmd": "ls"}
    assert any(isinstance(e, CompletionDone) for e in events)


def test_standard_openai_null_finish_reason_still_single_emit():
    """标准 OpenAI 语义（null 表示未结束）不受影响，保持单次发射。"""
    rows = [
        _chunk({"tool_calls": [_tools_arg("", call_id="call_s", name="read")]}, finish=None),
        _chunk({"tool_calls": [_tools_arg('{"f": "x"}')]}, finish=None),
        _chunk({"content": ""}, finish="tool_calls"),
        "data: [DONE]",
    ]

    tools = [e for e in _emit(rows) if isinstance(e, ToolUse)]

    assert len(tools) == 1
    assert tools[0].input == {"f": "x"}
