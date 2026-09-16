"""TUI 流式渲染:卡死看门狗 + 报错原因显示 的测试。

覆盖 tui/app.py 的 _consume_agent_stream 行为:
  - 事件流静默超过 stall_heartbeat → 打印"仍在等待"心跳(不再看似死机)
  - AgentError → 红色报错原因(message + code)被渲染
  - 通用异常 → 明失踪原因而非静默/裸回溯
"""

from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

from core.agent.events import AgentError, TextDelta
from tui.app import _StreamRenderer, _consume_agent_stream


class FakeAgent:
    """极简 agent 桩:只需 cancel()(Ctrl-C 路径用)。"""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeSpinner:
    """spinner 桩:cancel() 是 no-op(测试里不需要真动画面)。"""

    def cancel(self) -> None:
        pass

    def done(self) -> bool:
        return False


class FakeApp:
    """极简 app 桩:只暴露 _consume_agent_stream 用到的成员。"""

    def __init__(self):
        self.agent = FakeAgent()
        self.agent_running = True


def _make_renderer(console: Console) -> _StreamRenderer:
    return _StreamRenderer(console, spinner_task=_FakeSpinner())


async def _stall_then_text(delay: float) -> asyncio.AsyncGenerator:
    """先静默 delay 秒,再吐一条 TextDelta,再结束。"""
    await asyncio.sleep(delay)
    yield TextDelta(text="hi")
    return


async def _error_gen() -> asyncio.AsyncGenerator:
    """直接抛 AgentError 事件。"""
    yield AgentError(message="boom detail", code="stream_error")
    return


@pytest.mark.asyncio
async def test_stall_heartbeat_shows_waiting():
    """静默超过 stall_heartbeat 后,渲染输出里出现"仍在等待"。"""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    app = FakeApp()
    renderer = _make_renderer(console)

    # 静默 0.15s > stall_heartbeat=0.05s → 心跳触发;随后收到文本正常结束
    err, text = await _consume_agent_stream(
        app, _stall_then_text(0.15), renderer, console,
        stall_heartbeat=0.05, stall_warn_at=0.3,
        track_text=True,
    )
    rendered = buf.getvalue()
    assert "仍在等待" in rendered
    assert text == "hi"


@pytest.mark.asyncio
async def test_stall_long_triggers_warning():
    """静默超 stall_warn_at → 输出长期无进展警告。"""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    app = FakeApp()
    renderer = _make_renderer(console)

    # 静默 0.4s > warn_at=0.2s,且 > heartbeat。会连打心跳 + 触发警告。
    err, _ = await _consume_agent_stream(
        app, _stall_then_text(0.4), renderer, console,
        stall_heartbeat=0.05, stall_warn_at=0.2,
    )
    rendered = buf.getvalue()
    assert "长期无进展" in rendered


@pytest.mark.asyncio
async def test_agent_error_reason_shown():
    """AgentError → 输出含 message 与 code(报错原因明示)。"""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    app = FakeApp()
    renderer = _make_renderer(console)

    err, _ = await _consume_agent_stream(app, _error_gen(), renderer, console)
    rendered = buf.getvalue()
    assert "boom detail" in rendered
    assert "stream_error" in rendered
    assert err is True  # 报错 → error_occurred


@pytest.mark.asyncio
async def test_unexpected_exception_reason_shown():
    """事件流中途抛未知异常 → 输出原因,error_occurred=True。"""

    async def _boom():
        raise RuntimeError("network pipeline broke")
        yield TextDelta(text="unreachable")

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    app = FakeApp()
    renderer = _make_renderer(console)

    err, _ = await _consume_agent_stream(app, _boom(), renderer, console)
    rendered = buf.getvalue()
    assert "network pipeline broke" in rendered
    assert err is True


@pytest.mark.asyncio
async def test_normal_flow_no_error():
    """正常事件流(文本到达)不对负渲染错误、error_occurred=False。"""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100)
    app = FakeApp()
    renderer = _make_renderer(console)

    err, text = await _consume_agent_stream(
        app, _stall_then_text(0), renderer, console,
        stall_heartbeat=0.05, stall_warn_at=0.3,
        track_text=True,
    )
    assert err is False
    assert text == "hi"
