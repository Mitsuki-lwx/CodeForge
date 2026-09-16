"""LLM gen_ai.* OTel span 测试。

验证:
  1. 可观测性启用时,Session.stream_chat 发出名为 `chat.completions` 的 span,
     且带 gen_ai.system/model/operation/usage 键(供 Langfuse 识别为 LLM 调用)。
  2. 未启用时,span 是 no-op,事件流完全不受影响(现有客户端行为不回归)。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from core.observability import sdk

AUDIT_TRACES = "traces/data.jsonl"


@pytest.fixture
def obs_isolate(monkeypatch):
    """隔离的 obs 目录 + 重置 sdk/快照。"""

    from core.observability import providers

    d = Path(tempfile.mkdtemp())
    monkeypatch.setenv("CODEFORGE_OBS_DIR", str(d))
    with providers._snapshot_lock:
        providers._snapshot.clear()
    providers._counters.clear()
    sdk._initialized = False
    sdk._sinks.clear()
    yield d
    sdk.shutdown()
    sdk._initialized = False


def _read_first_span(obs_dir: Path) -> dict:
    p = obs_dir / AUDIT_TRACES
    if not p.exists():
        return {}
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("name") == "chat.completions":
            return d
    return {}


def test_gen_ai_span_emits_attributes(obs_isolate):
    """启用时:gen_ai span 带 system/model/operation/usage 键。"""
    from core.observability import ensure_initialized
    from core.observability import shutdown as obs_shutdown
    from llm.llm_span import gen_ai_span, wrap_events
    from llm.stream_events import CompletionDone

    assert ensure_initialized() is True

    async def fake_events():
        yield CompletionDone(
            usage={
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_input_tokens": 128,
                "cache_creation_input_tokens": 300,
            }
        )

    async def run():
        sc = gen_ai_span(protocol="openai", model="m", provider="p")
        async for _ in wrap_events(fake_events(), sc):
            pass

    asyncio.run(run())
    import time

    time.sleep(0.2)
    obs_shutdown()

    span = _read_first_span(obs_isolate)
    assert span, "no chat.completions span written"
    attrs = span.get("attributes", {})
    assert attrs.get("gen_ai.system") == "openai"
    assert attrs.get("gen_ai.request.model") == "m"
    assert attrs.get("gen_ai.operation.name") == "chat"
    assert attrs.get("gen_ai.usage.input_tokens") == 10
    assert attrs.get("gen_ai.usage.output_tokens") == 3
    assert attrs.get("gen_ai.usage.cache_read_input_tokens") == 128
    assert attrs.get("gen_ai.usage.cache_creation_input_tokens") == 300


def test_gen_ai_span_records_iteration_meta(obs_isolate):
    """设置了迭代元数据时,gen_ai span 带 codeforge.iteration / context_chars。"""
    from core.observability import ensure_initialized
    from core.observability import shutdown as obs_shutdown
    from core.observability.context import set_iteration_meta
    from llm.llm_span import gen_ai_span, wrap_events
    from llm.stream_events import CompletionDone

    assert ensure_initialized() is True
    set_iteration_meta(3, 1234)

    async def fake_events():
        yield CompletionDone(usage={"input_tokens": 1, "output_tokens": 1})

    async def run():
        sc = gen_ai_span(protocol="openai", model="m", provider="p")
        async for _ in wrap_events(fake_events(), sc):
            pass

    asyncio.run(run())
    import time

    time.sleep(0.2)
    obs_shutdown()

    span = _read_first_span(obs_isolate)
    assert span, "no chat.completions span written"
    attrs = span.get("attributes", {})
    assert attrs.get("codeforge.iteration") == 3
    assert attrs.get("codeforge.iteration_context_chars") == 1234


def test_no_otel_still_flows_events(monkeypatch):
    """未启用(no-op tracer):wrap_events 原样透传事件,不崩。"""
    from contextlib import nullcontext

    # 模拟可观测性未启用:get_tracer 返回 no-op nullcontext
    import core.observability.providers as prov
    from llm.llm_span import gen_ai_span, wrap_events
    from llm.stream_events import CompletionDone

    monkeypatch.setattr(prov, "get_tracer", lambda *a, **k: nullcontext())

    async def fake_events():
        yield CompletionDone(usage={"input_tokens": 5, "output_tokens": 2})

    got = []

    async def run():
        sc = gen_ai_span(protocol="openai", model="m", provider="p")
        async for ev in wrap_events(fake_events(), sc):
            got.append(ev)

    asyncio.run(run())
    assert len(got) == 1
    assert isinstance(got[0], CompletionDone)
    assert got[0].usage == {"input_tokens": 5, "output_tokens": 2}
