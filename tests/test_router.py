"""模型路由（spec_router）单测。"""

from __future__ import annotations

from config.model import ProviderConfig
from core.agent.router import judge_and_route, resolve_router


def _p(name, tier=""):
    return ProviderConfig(name=name, protocol="anthropic", model="m", api_key="k", tier=tier)


# ── resolve_router ─────────────────────────────────────────────────

def test_resolve_router_disabled_by_default():
    """默认关：即使配了 cheap+主，enabled=False 也不路由。"""
    cheap, main = _p("cheap", "cheap"), _p("main")
    assert resolve_router([cheap, main], main) is None  # 未传 enabled → 默认 False


def test_resolve_router_cheap_and_main():
    cheap, main = _p("cheap", "cheap"), _p("main")
    r = resolve_router([cheap, main], main, enabled=True)
    assert r == (cheap, main)


def test_resolve_router_custom_cheap_tier():
    cheap, main = _p("cheap", "fast"), _p("main")
    r = resolve_router([cheap, main], main, enabled=True, cheap_tier="fast")
    assert r == (cheap, main)


def test_resolve_router_no_cheap():
    main = _p("main")
    assert resolve_router([main], main, enabled=True) is None


def test_resolve_router_cheap_is_current_main():
    cheap = _p("cheap", "cheap")
    assert resolve_router([cheap], cheap, enabled=True) is None


def test_resolve_router_no_current():
    cheap = _p("cheap", "cheap")
    assert resolve_router([cheap], None, enabled=True) is None


# ── judge_and_route ────────────────────────────────────────────────

class _Ok:
    def __init__(self, script):
        self._script = script

    async def stream_chat(self, messages, system_prompt="", tools=None, **kw):
        from llm.stream_events import CompletionDone, TextChunk

        for step in self._script:
            if step[0] == "text":
                yield TextChunk(text=step[1])
            elif step[0] == "err":
                from llm.stream_events import StreamError

                yield StreamError(message="boom")
        yield CompletionDone(usage={})


class _Boom:
    async def stream_chat(self, *a, **k):
        raise RuntimeError("network down")


async def test_judge_simple(monkeypatch):
    import llm.client as client_mod

    cheap = _p("cheap", "cheap")
    monkeypatch.setattr(
        client_mod.LLMClient, "create", lambda cfg: _Ok([("text", "SIMPLE\n答案是 42")])
    )
    kind, answer = await judge_and_route(cheap, "1+1=?")
    assert kind == "simple"
    assert "答案是 42" in answer


async def test_judge_complex(monkeypatch):
    import llm.client as client_mod

    cheap = _p("cheap", "cheap")
    monkeypatch.setattr(
        client_mod.LLMClient, "create", lambda cfg: _Ok([("text", "COMPLEX")])
    )
    kind, answer = await judge_and_route(cheap, "重构这个函数")
    assert kind == "complex"
    assert answer is None


async def test_judge_stream_error_turns_complex(monkeypatch):
    import llm.client as client_mod

    cheap = _p("cheap", "cheap")
    monkeypatch.setattr(
        client_mod.LLMClient, "create", lambda cfg: _Ok([("err", "x")])
    )
    kind, _ = await judge_and_route(cheap, "hi")
    assert kind == "complex"


async def test_judge_exception_turns_complex(monkeypatch):
    import llm.client as client_mod

    cheap = _p("cheap", "cheap")
    monkeypatch.setattr(client_mod.LLMClient, "create", lambda cfg: _Boom())
    kind, _ = await judge_and_route(cheap, "hi")
    assert kind == "complex"


async def test_judge_empty_turns_complex(monkeypatch):
    import llm.client as client_mod

    cheap = _p("cheap", "cheap")
    monkeypatch.setattr(
        client_mod.LLMClient, "create", lambda cfg: _Ok([("text", "   ")])
    )
    kind, _ = await judge_and_route(cheap, "hi")
    assert kind == "complex"


async def test_judge_custom_prompt_replaces_placeholder(monkeypatch):
    import llm.client as client_mod

    cheap = _p("cheap", "cheap")
    seen = {}

    class _Rec:
        async def stream_chat(self, messages, system_prompt="", tools=None, **kw):
            seen["content"] = messages[0].content
            from llm.stream_events import CompletionDone, TextChunk

            yield TextChunk(text="SIMPLE\nok")
            yield CompletionDone(usage={})

    monkeypatch.setattr(client_mod.LLMClient, "create", lambda cfg: _Rec())
    await judge_and_route(cheap, "hi there", judge_prompt="自定义判断 {message}")
    assert "{message}" not in seen["content"]  # 占位符被替换
    assert "hi there" in seen["content"]


async def test_judge_custom_prompt_without_placeholder_appends(monkeypatch):
    import llm.client as client_mod

    cheap = _p("cheap", "cheap")
    seen = {}

    class _Rec:
        async def stream_chat(self, messages, system_prompt="", tools=None, **kw):
            seen["content"] = messages[0].content
            from llm.stream_events import CompletionDone, TextChunk

            yield TextChunk(text="COMPLEX")
            yield CompletionDone(usage={})

    monkeypatch.setattr(client_mod.LLMClient, "create", lambda cfg: _Rec())
    await judge_and_route(cheap, "qq", judge_prompt="直接判断")
    assert "qq" in seen["content"]  # 无占位符则追加用户请求
