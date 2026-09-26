"""`core/task/view.py` 纯渲染层单测。

覆盖：显示名取值顺序 / 耗时 / 列表（含序号稳定性、默认收敛、状态四值）/
transcript（消息类型映射、tail、截断）/ 选择器解析（序号、id、前缀、名字、歧义）。
"""

from __future__ import annotations

from conversation.manager import ConversationManager
from core.task.manager import BackgroundTask, TaskStatus
from core.task.view import (
    agent_line,
    disp_width,
    display_name,
    format_elapsed,
    message_lines,
    render_agent_list,
    render_transcript,
    resolve_selector,
    status_label,
)


def _bt(
    tid: str = "task_0001",
    *,
    name: str = "",
    task: str = "",
    status: TaskStatus = TaskStatus.RUNNING,
    start: float = 100.0,
    end: float = 0.0,
    steps: int = 0,
    activity: str = "",
    conv: ConversationManager | None = None,
) -> BackgroundTask:
    bt = BackgroundTask(
        id=tid,
        sub_agent=None,
        conv=conv if conv is not None else ConversationManager(),
        name=name,
        task=task,
        tool_count=steps,
        last_activity=activity,
        start_time=start,
    )
    bt.status = status
    bt.end_time = end
    return bt


# ── 显示名 / 耗时 ─────────────────────────────────────────────────


def test_display_name_prefers_name_then_task_then_id():
    assert display_name(_bt(name="alice", task="do thing")) == "alice"
    assert display_name(_bt(name="", task="第一行\n第二行")) == "第一行"
    assert display_name(_bt(name="", task="", tid="task_12def532")) == "12def532"


def test_display_name_clips_by_display_width():
    long_name = "一" * 30  # 60 列
    got = display_name(_bt(name=long_name), 10)
    assert disp_width(got) <= 10
    assert got.endswith("…")


def test_format_elapsed_boundaries():
    assert format_elapsed(0) == "0s"
    assert format_elapsed(12) == "12s"
    assert format_elapsed(59) == "59s"
    assert format_elapsed(60) == "1m00s"
    assert format_elapsed(83) == "1m23s"


def test_status_label_four_values():
    assert status_label(TaskStatus.RUNNING) == "运行中"
    assert status_label(TaskStatus.COMPLETED) == "已完成"
    assert status_label(TaskStatus.FAILED) == "失败"
    assert status_label(TaskStatus.CANCELLED) == "已取消"


# ── 列表 ─────────────────────────────────────────────────────────


def test_agent_line_has_all_segments():
    line = agent_line(3, _bt(name="alice", steps=8, activity="Grep"), now=223.0)
    assert "#3" in line
    assert "alice" in line
    assert "运行中" in line
    assert "2m03s" in line
    assert "8步" in line
    assert "Grep" in line


def test_agent_line_omits_zero_steps_and_empty_activity():
    line = agent_line(1, _bt(name="bob", steps=0, activity=""), now=100.0)
    assert "0步" not in line
    assert "排队" not in line
    assert line.endswith("0s")


def test_agent_line_shows_queued_count():
    line = agent_line(1, _bt(name="bob"), now=100.0, queued=2)
    assert "排队 2 条" in line


def test_list_empty_message():
    assert render_agent_list([]) == ["没有后台任务。"]


def test_list_header_counts_running():
    tasks = [
        _bt("task_a", name="a", status=TaskStatus.RUNNING),
        _bt("task_b", name="b", status=TaskStatus.COMPLETED, end=101.0),
    ]
    lines = render_agent_list(tasks, now=110.0)
    assert lines[0] == "后台任务 2（运行中 1）"


def test_list_index_is_position_even_when_status_changes():
    """★ 序号 = 全量列表位置：任务状态变化**不会**让序号指向别的任务。"""
    first = _bt("task_a", name="a", status=TaskStatus.RUNNING)
    second = _bt("task_b", name="b", status=TaskStatus.RUNNING)
    lines = render_agent_list([first, second], now=110.0)
    assert "  #1 " in lines[1] and "a" in lines[1]
    assert "  #2 " in lines[2] and "b" in lines[2]

    first.status = TaskStatus.COMPLETED
    first.end_time = 105.0
    lines2 = render_agent_list([first, second], now=110.0)
    assert "  #1 " in lines2[1] and "a" in lines2[1]
    assert "已完成" in lines2[1]


def test_list_default_hides_old_finished_and_says_so():
    tasks = [
        _bt(f"task_{i}", name=f"n{i}", status=TaskStatus.COMPLETED, end=110.0)
        for i in range(8)
    ]
    tasks.append(_bt("task_run", name="runner", status=TaskStatus.RUNNING))
    lines = render_agent_list(tasks, now=120.0)

    body = "\n".join(lines)
    assert "runner" in body  # 运行中的永远显示
    assert "n1" not in body  # 最早的已完成被收敛掉
    assert "另有 3 个更早的任务未显示：/agents all" in body


def test_list_show_all_keeps_everything_and_no_hint():
    tasks = [
        _bt(f"task_{i}", name=f"n{i}", status=TaskStatus.COMPLETED, end=110.0)
        for i in range(8)
    ]
    lines = render_agent_list(tasks, show_all=True, now=120.0)
    body = "\n".join(lines)
    for i in range(8):
        assert f"n{i}" in body
    assert "另有" not in body


def test_list_finished_limit_zero_keeps_only_running():
    tasks = [
        _bt("task_a", name="dead", status=TaskStatus.COMPLETED, end=1.0),
        _bt("task_b", name="alive", status=TaskStatus.RUNNING),
    ]
    lines = render_agent_list(tasks, finished_limit=0, now=10.0)
    body = "\n".join(lines)
    assert "alive" in body
    assert "dead" not in body


def test_list_shows_queued_from_mapping():
    tasks = [_bt("task_q", name="alice")]
    lines = render_agent_list(tasks, now=105.0, queued={"task_q": 3})
    assert "排队 3 条" in "\n".join(lines)


def test_list_footer_has_usage_hint():
    lines = render_agent_list([_bt(name="a")], now=101.0)
    assert "/agents show" in lines[-1]


# ── transcript ───────────────────────────────────────────────────


def _conv_with_all_kinds() -> ConversationManager:
    conv = ConversationManager()
    conv.add_user_message("按顺序做三步")
    conv.add_assistant_message("我先看下目录")
    conv.add_tool_use("t1", "glob", {"pattern": "**/*.py", "n": 3})
    conv.add_tool_result("t1", "line1\nline2")
    conv.add_system_reminder("别越界")
    conv.add_assistant_message("读完了")
    return conv


def test_transcript_header_and_mapping():
    bt = _bt(
        name="alice",
        task="按顺序做三步",
        steps=4,
        activity="Grep",
        conv=_conv_with_all_kinds(),
    )
    lines = render_transcript(bt, index=2, now=160.0)
    body = "\n".join(lines)

    assert lines[0].startswith("#2  alice")
    assert "运行中" in lines[0]
    assert "1m00s" in lines[0]
    assert "4步" in lines[0]
    assert "任务：按顺序做三步" in lines[0]
    assert "You     : 按顺序做三步" in body
    assert "Agent   : 我先看下目录" in body
    assert "tool    : glob(pattern='**/*.py', n=3)" in body
    assert "result  : line1" in body
    assert "sys     : 别越界" in body
    assert "（共 6 条消息）" in body


def test_transcript_multiline_content_is_indented_block():
    conv = ConversationManager()
    conv.add_assistant_message("第一行\n第二行")
    lines = render_transcript(_bt(conv=conv))
    assert "  Agent   : 第一行" in lines
    assert "  " + " " * 8 + ": 第二行" in lines


def test_transcript_tail_limits_messages_and_hints():
    bt = _bt(name="a", conv=_conv_with_all_kinds())
    lines = render_transcript(bt, index=1, tail=2)
    body = "\n".join(lines)
    assert "工具" not in body
    assert "（共 6 条消息，显示最近 2 条" in body
    assert "--tail 6" in body


def test_transcript_full_disables_clipping():
    conv = ConversationManager()
    conv.add_tool_result("t1", "x" * 2500)
    clipped = render_transcript(_bt(conv=conv), full=False)
    full = render_transcript(_bt(conv=conv), full=True)
    assert any("显示前 1000" in line for line in clipped)
    assert not any("显示前 1000" in line for line in full)
    assert any(len(line) > 2000 for line in full)


def test_transcript_empty_history():
    lines = render_transcript(_bt(name="a"))
    assert any("（还没有消息）" in line for line in lines)


def test_transcript_skips_empty_assistant_message():
    conv = ConversationManager()
    conv.start_assistant_stream()  # content 为空的流式占位
    assert message_lines(conv.messages[0]) == []


# ── 选择器 ───────────────────────────────────────────────────────


def _three():
    return [
        _bt("task_1111aaaa", name="alice"),
        _bt("task_2222bbbb", name="bob"),
        _bt("task_3333cccc", name=""),
    ]


def test_resolve_by_index():
    r = resolve_selector("2", _three())
    assert r.ok and r.index == 2 and r.task.name == "bob"


def test_resolve_index_out_of_range():
    r = resolve_selector("9", _three())
    assert not r.ok
    assert "超出范围" in r.error
    assert "共 3 个任务" in r.error


def test_resolve_by_full_id_and_prefix():
    tasks = _three()
    assert resolve_selector("task_2222bbbb", tasks).task.name == "bob"
    assert resolve_selector("task_2222", tasks).task.name == "bob"


def test_resolve_by_name():
    r = resolve_selector("alice", _three())
    assert r.ok and r.index == 1


def test_resolve_not_found_and_empty_usage():
    assert not resolve_selector("ghost", _three()).ok
    empty = resolve_selector("", _three(), usage="用法：/agents show <sel>")
    assert not empty.ok
    assert empty.error == "用法：/agents show <sel>"


def test_resolve_ambiguous_prefix_lists_candidates():
    tasks = [_bt("task_aaaa1"), _bt("task_aaaa2")]
    r = resolve_selector("task_aaaa", tasks)
    assert not r.ok
    assert "歧义" in r.error
    assert "#1" in r.error and "#2" in r.error


def test_resolve_ambiguous_name():
    tasks = [_bt("task_1", name="dup"), _bt("task_2", name="dup")]
    r = resolve_selector("dup", tasks)
    assert not r.ok
    assert "歧义" in r.error


def test_render_is_pure():
    bt = _bt(name="a", steps=2, activity="Grep", conv=_conv_with_all_kinds())
    before = (bt.name, bt.tool_count, bt.last_activity, bt.status, bt.end_time)
    a = render_transcript(bt, index=1, now=200.0)
    b = render_transcript(bt, index=1, now=200.0)
    assert a == b
    assert (bt.name, bt.tool_count, bt.last_activity, bt.status, bt.end_time) == before
