"""`/agents` 命令分发单测（`core/commands/builtin_agents.py`）。

命令 handler 只做分发 —— 真正的渲染与引擎调用在 UI 协议实现里，
所以这里用记录型 UI 断言"调了哪个能力、参数对不对、回显到哪个通道"。
"""

from __future__ import annotations

import pytest

from core.commands.builtin_agents import _parse_show, handle_agents
from core.commands.ui import NopUI


class _RecordingUI(NopUI):
    """记录型 UI：把调用参数与输出都记下来。"""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.errs: list[str] = []
        self.calls: list[tuple] = []
        self.stop_reply = "已发出停止请求：#3 alice"
        self.tell_reply = "已续派给 #3 alice"

    def println(self, msg: str) -> None:
        self.out.append(msg)

    def error(self, msg: str) -> None:
        self.errs.append(msg)

    def print_markup(self, msg: str) -> None:
        self.out.append(msg)

    def agent_list_lines(self, show_all: bool = False) -> list[str]:
        self.calls.append(("list", show_all))
        return ["后台任务 1（运行中 1）", "  #1  alice  运行中  3s"]

    def agent_show_lines(
        self, sel: str, tail: int = 20, full: bool = False
    ) -> list[str]:
        self.calls.append(("show", sel, tail, full))
        return [f"transcript of {sel}"]

    async def agent_stop(self, sel: str) -> str:
        self.calls.append(("stop", sel))
        return self.stop_reply

    async def agent_tell(self, sel: str, message: str) -> str:
        self.calls.append(("tell", sel, message))
        return self.tell_reply


@pytest.mark.asyncio
async def test_default_is_list():
    ui = _RecordingUI()
    await handle_agents(ui)
    assert ui.calls == [("list", False)]
    assert ui.out[0] == "后台任务 1（运行中 1）"
    assert ui.errs == []


@pytest.mark.asyncio
async def test_all_passes_show_all():
    ui = _RecordingUI()
    await handle_agents(ui, "all")
    assert ui.calls == [("list", True)]


@pytest.mark.asyncio
async def test_show_with_flags():
    ui = _RecordingUI()
    await handle_agents(ui, "show 3 --tail 5 --full")
    assert ui.calls == [("show", "3", 5, True)]
    assert ui.out == ["transcript of 3"]


@pytest.mark.asyncio
async def test_show_flag_before_selector():
    ui = _RecordingUI()
    await handle_agents(ui, "show --full 3")
    assert ui.calls == [("show", "3", 20, True)]


@pytest.mark.asyncio
async def test_show_tail_equals_form():
    ui = _RecordingUI()
    await handle_agents(ui, "show alice --tail=7")
    assert ui.calls == [("show", "alice", 7, False)]


@pytest.mark.asyncio
async def test_show_without_selector_errors_and_does_not_call():
    ui = _RecordingUI()
    await handle_agents(ui, "show")
    assert ui.calls == []
    assert ui.errs and "Usage: /agents show" in ui.errs[0]


@pytest.mark.asyncio
async def test_show_usage_is_missing_selector_only():
    """`show --tail 5` 没有选择器 —— 不能被当成合法调用。"""
    ui = _RecordingUI()
    await handle_agents(ui, "show --tail 5")
    assert ui.calls == []
    assert ui.errs


@pytest.mark.asyncio
async def test_stop_prints_reply():
    ui = _RecordingUI()
    await handle_agents(ui, "stop 3")
    assert ui.calls == [("stop", "3")]
    assert ui.out == ["已发出停止请求：#3 alice"]


@pytest.mark.asyncio
async def test_stop_without_selector_errors():
    ui = _RecordingUI()
    await handle_agents(ui, "stop")
    assert ui.calls == []
    assert "Usage: /agents stop" in ui.errs[0]


@pytest.mark.asyncio
async def test_tell_keeps_spaces_in_message():
    ui = _RecordingUI()
    await handle_agents(ui, "tell 3 先别写代码 先把测试跑通")
    assert ui.calls == [("tell", "3", "先别写代码 先把测试跑通")]


@pytest.mark.asyncio
async def test_tell_without_message_errors():
    ui = _RecordingUI()
    await handle_agents(ui, "tell 3")
    assert ui.calls == []
    assert "Usage: /agents tell" in ui.errs[0]


@pytest.mark.asyncio
async def test_tell_error_reply_goes_to_error_channel():
    ui = _RecordingUI()
    ui.tell_reply = "x 未找到任务 'ghost'"
    await handle_agents(ui, "tell ghost hi")
    assert ui.out == []
    assert ui.errs == ["未找到任务 'ghost'"]


@pytest.mark.asyncio
async def test_unknown_subcommand_lists_available():
    ui = _RecordingUI()
    await handle_agents(ui, "nope")
    assert ui.calls == []
    assert "Unknown subcommand: /agents nope" in ui.errs[0]
    assert "show" in ui.errs[0]


def test_parse_show_defaults_on_bad_values():
    assert _parse_show("3") == ("3", 20, False)
    assert _parse_show("3 --tail abc") == ("3", 20, False)
    assert _parse_show("3 --tail 0") == ("3", 20, False)
    assert _parse_show("3 --tail -4") == ("3", 20, False)
    assert _parse_show("") == ("", 20, False)
