"""`/show` 渲染层的验收（`docs/checklist_show_redact.md` §1、§2）。

重点验两件容易悄悄做错的事：
1. **轮切分的边界**（spec §4.3 逐条定死了，不许猜）
2. **与 `render_transcript` 逐行同构**（不许出现第二份消息渲染实现）
"""

from __future__ import annotations

import copy

from conversation.message import Message, MessageRole
from core.task.view import (
    MESSAGE_MAX_CHARS,
    render_cost_summary,
    render_round_overview,
    render_session_transcript,
    render_transcript,
    split_rounds,
)


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _agent(text: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text)


def _tool(tid: str, name: str, args: dict) -> Message:
    return Message(
        role=MessageRole.ASSISTANT, content="", tool_use_id=tid, tool_name=name,
        tool_input=args,
    )


def _result(tid: str, out: str) -> Message:
    return Message(role=MessageRole.USER, content=out, tool_use_id=tid)


def _reminder(text: str) -> Message:
    return _user(f"[system_reminder] {text}[/system_reminder]")


# ── §1 轮切分边界（spec §4.3 逐条）──────────────────────────


def test_two_user_messages_make_two_rounds() -> None:
    rs = split_rounds([_user("a"), _agent("b"), _user("c")])
    assert [r.number for r in rs] == [1, 2]
    assert rs[0].prompt == "a"
    assert rs[1].prompt == "c"


def test_consecutive_user_messages_do_not_swallow_the_second() -> None:
    """连续两个 `You:` ⇒ 前一轮在 `You:` 处**结束**，后一轮不丢。"""
    rs = split_rounds([_user("a"), _user("b")])
    assert len(rs) == 2
    assert [r.prompt for r in rs] == ["a", "b"]


def test_system_reminder_does_not_start_a_round() -> None:
    rs = split_rounds([_user("a"), _reminder("别忘了"), _agent("done")])
    assert len(rs) == 1
    assert rs[0].prompt == "a"


def test_leading_tool_calls_join_round_one() -> None:
    """开头的工具调用（没有前导用户消息）并入第 1 轮，不被丢掉。"""
    rs = split_rounds([_tool("t1", "Grep", {"p": "x"}), _result("t1", "3 matches")])
    assert len(rs) == 1
    assert rs[0].tool_count == 1
    assert rs[0].messages[0].tool_name == "Grep"


def test_tool_result_does_not_start_a_round() -> None:
    """工具结果也是 user 角色，但不能被当成"人新一轮说话"。"""
    rs = split_rounds([_user("a"), _tool("t1", "Bash", {}), _result("t1", "out")])
    assert len(rs) == 1


def test_empty_list_gives_no_rounds() -> None:
    assert split_rounds([]) == []


def test_blank_user_message_does_not_start_a_round() -> None:
    rs = split_rounds([_user("a"), _user("   ")])
    assert len(rs) == 1


# ── §1 Round 属性 ─────────────────────────────────────────────


def test_round_tool_count_counts_calls_not_results() -> None:
    r = split_rounds(
        [_user("a"), _tool("t1", "Grep", {}), _result("t1", "x"), _tool("t2", "Edit", {}),
         _result("t2", "y")]
    )[0]
    assert r.tool_count == 2, "结果消息不算调用"
    assert r.tool_names == ["Grep", "Edit"]


def test_round_prompt_is_empty_when_no_user_text() -> None:
    r = split_rounds([_tool("t1", "Grep", {})])[0]
    assert r.prompt == ""
    assert "（无用户输入）" in r.summary_line()


def test_round_summary_line_omits_empty_segments() -> None:
    r = split_rounds([_user("问个问题")])[0]
    line = r.summary_line()
    assert "问个问题" in line
    assert "个工具" not in line, "0 个工具不该出现"
    assert "·" not in line  # 无任何尾段时无多余分隔符


# ── §1 复用：与 render_transcript 逐行同构 ─────────────────────


class _Conv:
    def __init__(self, msgs):
        self.messages = msgs


class _Bt:
    """最小 BackgroundTask 替身（只需 view.py 读到的属性）。"""

    def __init__(self, msgs, name="x"):
        self.conv = _Conv(msgs)
        self.name = name
        self.task = "t"
        self.status = "COMPLETED"
        self.start_time = 0.0
        self.end_time = 1.0
        self.tool_count = 1


def test_message_rendering_is_identical_to_task_transcript() -> None:
    """同一批消息，两者渲染出的**消息行必须逐行相同**（§4.2 硬约束）。"""
    msgs = [
        _user("找一下"),
        _tool("t1", "Grep", {"pattern": "foo"}),
        _result("t1", "3 matches in 2 files"),
        _agent("找到了。"),
    ]
    task_lines = render_transcript(_Bt(msgs), index=1, tail=0)
    # 去掉头 2 行与尾注，只留消息体
    task_body = task_lines[2:-1]
    session_body = render_session_transcript(msgs, index=1, tail=0)[2:]
    assert task_body == session_body


def test_render_transcript_default_hint_unchanged() -> None:
    """★ 不传 hint_more 时，尾注必须与改动前**逐字相同**。"""
    msgs = [_user("a")] * 30
    lines = render_transcript(_Bt(msgs), index=2, tail=5)
    assert "调整：/agents show 2 --tail 30）" in lines[-1]


def test_render_transcript_accepts_custom_hint() -> None:
    msgs = [_user("a")] * 30
    lines = render_transcript(_Bt(msgs), index=2, tail=5, hint_more="/show --tail 30")
    assert "/agents show" not in lines[-1]
    assert "/show --tail 30" in lines[-1]


# ── §1 渲染 ───────────────────────────────────────────────────


def test_tool_line_includes_arguments() -> None:
    msgs = [_user("a"), _tool("t1", "Grep", {"pattern": "needle", "path": "src"})]
    out = "\n".join(render_session_transcript(msgs, index=1))
    assert "tool" in out
    assert "Grep" in out
    assert "pattern=" in out and "needle" in out


def test_result_line_uses_full_content_not_preview() -> None:
    """工具结果必须来自完整 content，而不是 120 字 preview。"""
    long = "X" * 500
    msgs = [_user("a"), _tool("t1", "Bash", {}), _result("t1", long)]
    out = "\n".join(render_session_transcript(msgs, index=1, full=True))
    assert long in out, "--full 应拿到完整结果"


def test_default_truncates_at_message_max_chars() -> None:
    long = "Y" * (MESSAGE_MAX_CHARS + 500)
    msgs = [_user("a"), _result("t1", long)]
    out = "\n".join(render_session_transcript(msgs, index=1))
    assert long not in out
    assert f"（共 {len(long)} 字符" in out


def test_only_tools_filters_dialogue_but_keeps_the_question() -> None:
    """`--tools` 滤掉对话正文，但**保留头部的"问：…"**。

    ★ 刻意如此：不留这行的话，用户看到一串工具调用却不知道自己问的是什么 ——
    而"知道自己问了什么"正是 `/show` 要补的信息缺失（spec §1.1）。
    """
    msgs = [_user("问题在这里"), _tool("t1", "Grep", {}), _result("t1", "out"), _agent("回答")]
    out = "\n".join(render_session_transcript(msgs, index=1, only_tools=True))
    assert "Grep" in out and "out" in out
    assert "问：问题在这里" in out, "头部应保留问题摘要"
    assert "回答" not in out, "助手回复应被滤掉"
    # 滤掉的是正文里的 You 行（头部那句不算）
    assert "You " not in out


def test_only_tools_with_no_tools_says_so() -> None:
    msgs = [_user("只是问了一句"), _agent("答")]
    lines = render_session_transcript(msgs, index=1, only_tools=True)
    assert any("没有工具调用" in line for line in lines)


def test_index_out_of_range_is_clear_error() -> None:
    msgs = [_user("a")]
    assert "超出范围" in render_session_transcript(msgs, index=99)[0]
    assert "超出范围" in render_session_transcript(msgs, index=0)[0]


def test_empty_messages_says_nothing_yet() -> None:
    assert "还没有消息" in render_session_transcript([], index=1)[0]


def test_render_is_pure() -> None:
    """纯函数：同输入两次一致，且不改传入对象。"""
    msgs = [_user("a"), _tool("t1", "Grep", {"p": "x"}), _result("t1", "y")]
    before = copy.deepcopy([(m.role, m.content, m.tool_name) for m in msgs])
    a = render_session_transcript(msgs, index=1, full=True)
    b = render_session_transcript(msgs, index=1, full=True)
    assert a == b
    assert [(m.role, m.content, m.tool_name) for m in msgs] == before


# ── §2 概览 ───────────────────────────────────────────────────


def test_overview_lists_rounds_with_tool_count() -> None:
    msgs = [
        _user("第一个问题"),
        _tool("t1", "Grep", {}),
        _user("第二个问题"),
    ]
    out = "\n".join(render_round_overview(msgs))
    assert "本会话 2 轮" in out
    assert "第一个问题" in out
    assert "第二个问题" in out
    assert "1个工具 Grep" in out


def test_overview_tail_limits_rows_and_says_so() -> None:
    msgs = [_user(f"q{i}") for i in range(10)]
    out = "\n".join(render_round_overview(msgs, tail=3))
    assert "本会话 10 轮（显示最近 3 轮）" in out
    assert "q9" in out
    assert "q0" not in out


def test_overview_tail_zero_shows_all() -> None:
    msgs = [_user(f"q{i}") for i in range(5)]
    out = "\n".join(render_round_overview(msgs, tail=0))
    assert "q0" in out and "q4" in out


def test_overview_mentions_earlier_rounds_hint() -> None:
    msgs = [_user(f"q{i}") for i in range(10)]
    out = "\n".join(render_round_overview(msgs, tail=2))
    assert "更早的 8 轮未显示" in out


def test_overview_empty_session() -> None:
    assert "还没有" in render_round_overview([])[0]


def test_overview_truncates_long_prompt() -> None:
    msgs = [_user("长" * 300)]
    line = next(l for l in render_round_overview(msgs) if "长" in l)
    assert len(line) < 120, "长提问必须截断，不能刷屏"


def test_overview_accepts_elapsed_per_round() -> None:
    msgs = [_user("a")]
    out = "\n".join(render_round_overview(msgs, elapsed_by_round={1: 83.0}))
    assert "1m23s" in out


# ── §2 成本汇总 ───────────────────────────────────────────────


def test_cost_summary_shows_tokens_and_tools() -> None:
    out = "\n".join(
        render_cost_summary(tokens_in=1234, tokens_out=567, tool_calls=8, elapsed_s=83.0)
    )
    assert "1,234" in out and "567" in out
    assert "1,801" in out  # 1234 + 567
    assert "工具调用     8" in out
    assert "1m23s" in out


def test_cost_summary_flags_plaintext_when_redaction_off() -> None:
    off = "\n".join(
        render_cost_summary(tokens_in=0, tokens_out=0, tool_calls=0, redaction_on=False)
    )
    on = "\n".join(
        render_cost_summary(tokens_in=0, tokens_out=0, tool_calls=0, redaction_on=True)
    )
    assert "明文落盘" in off
    assert "明文落盘" not in on
    assert "凭据已遮蔽" in on


def test_cost_summary_zero_values_not_hidden() -> None:
    """与 /status 的"0 不显示"不同：这里是显式用量汇总，0 就是 0。"""
    out = "\n".join(render_cost_summary(tokens_in=0, tokens_out=0, tool_calls=0))
    assert "输入 token   0" in out
