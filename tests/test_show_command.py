"""`/show` 命令层的验收（`docs/checklist_show_redact.md` §3）。

这里验的是**参数解析与形态分发** —— 渲染细节在 `test_show_view.py`。
"""

from __future__ import annotations

import pytest

from core.commands.builtin_show import handle_show
from core.commands.ui import NopUI
from tui import show_view
from tui.show_view import parse_show_args


class _Rec(NopUI):
    """记录输出的 NopUI。"""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []

    def print_markup(self, msg: str) -> None:
        self.out.append(msg)

    def error(self, msg: str) -> None:
        self.err.append(msg)


class _Conv:
    def __init__(self, msgs):
        self.messages = msgs


class _App:
    """最小 app 替身：只需要 conversation（+ session_id 供工具计数降级）。"""

    def __init__(self, msgs, session_id: str = "s1"):
        self.conversation = _Conv(msgs)
        self._sid = session_id

    def session_id(self) -> str:
        return self._sid


def _user(t):
    from conversation.message import Message, MessageRole

    return Message(role=MessageRole.USER, content=t)


def _agent(t):
    from conversation.message import Message, MessageRole

    return Message(role=MessageRole.ASSISTANT, content=t)


def _tool(tid, name):
    from conversation.message import Message, MessageRole

    return Message(
        role=MessageRole.ASSISTANT, content="", tool_use_id=tid, tool_name=name,
        tool_input={},
    )


# ── 参数解析 ───────────────────────────────────────────────────


def test_parse_plain_selector() -> None:
    assert parse_show_args("3") == ("3", 0, False, False, False)


def test_parse_all_flags() -> None:
    assert parse_show_args("2 --full --tools") == ("2", 0, True, True, False)
    assert parse_show_args("--cost") == ("", 0, False, False, True)


@pytest.mark.parametrize("raw", ["--tail 5", "--tail=5"])
def test_parse_tail_forms(raw: str) -> None:
    assert parse_show_args(raw)[1] == 5


def test_parse_selector_can_appear_after_flags() -> None:
    """选择器位置无关（`--full 3` 也认）—— 与 `/agents show` 同一约定。"""
    assert parse_show_args("--full 3")[0] == "3"
    assert parse_show_args("--full 3")[2] is True


def test_parse_rejects_non_positive_tail() -> None:
    assert parse_show_args("--tail 0")[1] == 0
    assert parse_show_args("--tail -3")[1] == 0
    assert parse_show_args("--tail abc")[1] == 0


def test_parse_empty() -> None:
    assert parse_show_args("") == ("", 0, False, False, False)


def test_parse_dash_prefixed_token_is_not_a_selector() -> None:
    assert parse_show_args("--nope 3")[0] == "3"


# ── 形态分发 ───────────────────────────────────────────────────


def test_bare_show_gives_overview() -> None:
    app = _App([_user("第一个"), _tool("t1", "Grep"), _user("第二个")])
    lines = show_view.render(app, "")
    assert any("本会话 2 轮" in line for line in lines)
    assert any("第一个" in line for line in lines)


def test_show_n_gives_that_round() -> None:
    app = _App([_user("第一个"), _agent("答一"), _user("第二个"), _agent("答二")])
    out = "\n".join(show_view.render(app, "2"))
    assert "第 2/2 轮" in out
    assert "答二" in out
    assert "答一" not in out


def test_show_out_of_range_is_error_line() -> None:
    app = _App([_user("唯一一轮")])
    assert show_view.render(app, "99")[0].startswith("x ")


def test_show_non_numeric_selector_is_error() -> None:
    app = _App([_user("a")])
    out = show_view.render(app, "abc")
    assert out[0].startswith("x ")
    assert "正整数" in out[0]


def test_show_tools_filters_but_keeps_question() -> None:
    from conversation.message import Message, MessageRole

    app = _App([
        _user("问题在这"),
        _tool("t1", "Grep"),
        Message(role=MessageRole.USER, content="匹配结果", tool_use_id="t1"),
        _agent("答复"),
    ])
    out = "\n".join(show_view.render(app, "1 --tools"))
    assert "Grep" in out and "匹配结果" in out
    assert "问：问题在这" in out
    assert "答复" not in out


def test_show_full_does_not_truncate() -> None:
    from conversation.message import Message, MessageRole

    long = "Z" * 2000
    app = _App([
        _user("a"),
        Message(role=MessageRole.USER, content=long, tool_use_id="t1"),
    ])
    assert long in "\n".join(show_view.render(app, "1 --full"))
    assert long not in "\n".join(show_view.render(app, "1"))


def test_show_tail_limits_overview() -> None:
    app = _App([_user(f"q{i}") for i in range(20)])
    out = "\n".join(show_view.render(app, "--tail 3"))
    assert "本会话 20 轮（显示最近 3 轮）" in out


def test_show_cost_reports_usage_and_redaction() -> None:
    app = _App([_user("a"), _tool("t1", "Grep")])
    app.agent = type("A", (), {"_total_usage": {"input_tokens": 100, "output_tokens": 20}})()
    out = "\n".join(show_view.render(app, "--cost"))
    assert "输入 token   100" in out
    assert "落盘脱敏" in out


def test_show_cost_works_without_agent_attr() -> None:
    """拿不到 usage 时不能崩（退化成 0）。"""
    app = _App([_user("a")])
    out = "\n".join(show_view.render(app, "--cost"))
    assert "输入 token   0" in out


# ── handler ───────────────────────────────────────────────────


async def test_handler_prints_lines() -> None:
    ui = _Rec()
    ui.show_lines = lambda args: ["line1", "line2"]  # type: ignore[method-assign]
    await handle_show(ui, "")
    assert ui.out == ["line1", "line2"]
    assert ui.err == []


async def test_handler_routes_x_lines_to_error() -> None:
    ui = _Rec()
    ui.show_lines = lambda args: ["ok", "x 出错了"]  # type: ignore[method-assign]
    await handle_show(ui, "")
    assert ui.out == ["ok"]
    assert ui.err == ["出错了"]


async def test_handler_via_real_app_rejects_bad_selector() -> None:
    """走完整 handler + show_view.render。"""
    ui = _Rec()
    app = _App([_user("a")])
    ui.show_lines = lambda args: show_view.render(app, args)  # type: ignore[method-assign]
    await handle_show(ui, "abc")
    assert ui.err and "正整数" in ui.err[0]


# ── 边界：app 结构不完整时不能崩 ──────────────────────────────


def test_app_without_conversation_returns_empty() -> None:
    class Broken:
        pass

    assert show_view.messages_of(Broken()) == []
    assert show_view.render(Broken(), "") == ["还没有可展开的轮次。"]


def test_conversation_raising_is_contained() -> None:
    class Boom:
        @property
        def messages(self):
            raise RuntimeError("boom")

    class App:
        conversation = Boom()

    assert show_view.messages_of(App()) == []
