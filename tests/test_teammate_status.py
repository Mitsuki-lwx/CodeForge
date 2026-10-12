"""后台任务 / 队友状态行 测试。

覆盖三块：

1. 纯函数 `build_status_text` / `format_elapsed` / `disp_width`
   —— 含中英混排宽度与超宽截断；
2. **真实渲染管线**：用 prompt_toolkit 的 pipe input + `PlainTextOutput` 真跑一遍，
   断言状态行**真的出现在渲染产物里**（不是"我认为会显示"），无任务时**不出现**；
3. 刷新协程：只在文本变化时触发重绘（否则引入常驻重绘开销）。

对应 `docs/spec_teammate_status.md` / `docs/checklist_teammate_status.md`。
"""

from __future__ import annotations

import asyncio
import io

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.plain_text import PlainTextOutput

from core.task.manager import BackgroundTask, TaskStatus
from tui.session import _STATUS_TICK, _refresh_status_bar
from tui.teammate_status import build_status_text, disp_width, format_elapsed

NOW = 1000.0


class _Mgr:
    """最小 task_mgr 替身：只需 `list()`。"""

    def __init__(self, tasks: list | None = None) -> None:
        self.tasks = list(tasks or [])

    def list(self) -> list:
        return list(self.tasks)


def _bt(
    name: str = "alice",
    *,
    status: TaskStatus = TaskStatus.RUNNING,
    start: float = 990.0,
    end: float = 0.0,
    steps: int = 0,
    activity: str = "",
    tid: str = "task_ab12cd34",
    task: str = "",
) -> BackgroundTask:
    return BackgroundTask(
        id=tid,
        sub_agent=None,
        conv=None,
        name=name,
        status=status,
        start_time=start,
        end_time=end,
        tool_count=steps,
        last_activity=activity,
        task=task,
    )


# ── 1. 纯函数 ──────────────────────────────────────────────────────


def test_no_running_task_returns_none():
    """★ 没有任何运行中任务 → `None`（不占行 = 与引入前逐字一致）。"""
    assert build_status_text(_Mgr(), now=NOW, width=80) is None
    assert (
        build_status_text(_Mgr([_bt(status=TaskStatus.COMPLETED)]), now=NOW, width=80)
        is None
    )


def test_none_manager_is_safe():
    assert build_status_text(None, now=NOW, width=80) is None


def test_single_task_format():
    text = build_status_text(_Mgr([_bt(steps=8, activity="Grep")]), now=NOW, width=80)
    assert text == "后台 1 · alice 10s·8步·Grep"


def test_multiple_tasks_ordered_by_start():
    mgr = _Mgr(
        [
            _bt("alice", start=990.0, steps=8, activity="Grep"),
            _bt("bob", start=997.0, tid="task_11223344"),
        ]
    )
    assert (
        build_status_text(mgr, now=NOW, width=80)
        == "后台 2 · alice 10s·8步·Grep · bob 3s"
    )


def test_zero_steps_and_empty_activity_omitted():
    """不显示 `0步` 这类噪音。"""
    text = build_status_text(_Mgr([_bt(steps=0, activity="")]), now=NOW, width=80)
    assert text == "后台 1 · alice 10s"
    assert "0步" not in (text or "")


def test_unnamed_task_prefers_task_text_over_id():
    """★ `name` 未给时退回**任务文本首行**，而不是 hex 短码。

    实测（真链路）：`AgentTool` 的 `name` 是可选参数，后台子 Agent 常常没名字，
    状态行只剩 `12def532` —— 对"看得见在干什么"毫无帮助。
    """
    with_task = _bt("", task="重构 auth 模块\n并补测试")
    assert build_status_text(_Mgr([with_task]), now=NOW, width=80) == (
        "后台 1 · 重构 auth 模块 10s"
    )


def test_unnamed_task_without_task_text_falls_back_to_id():
    """连任务文本都没有 → 才用 id 短码。"""
    assert (
        build_status_text(_Mgr([_bt("")]), now=NOW, width=80) == "后台 1 · ab12cd34 10s"
    )


def test_non_running_statuses_excluded():
    """只显示 RUNNING —— 完成/失败已有 `<task-notification>` 通路。"""
    mgr = _Mgr(
        [
            _bt("done", status=TaskStatus.COMPLETED),
            _bt("bad", status=TaskStatus.FAILED, tid="task_22222222"),
            _bt("stopped", status=TaskStatus.CANCELLED, tid="task_33333333"),
            _bt("running", steps=1, tid="task_44444444"),
        ]
    )
    text = build_status_text(mgr, now=NOW, width=80)
    assert text == "后台 1 · running 10s·1步"
    for hidden in ("done", "bad", "stopped"):
        assert hidden not in (text or "")


def test_elapsed_format_boundaries():
    assert format_elapsed(0) == "0s"
    assert format_elapsed(12.9) == "12s"
    assert format_elapsed(59) == "59s"
    assert format_elapsed(60) == "1m00s"
    assert format_elapsed(83) == "1m23s"


def test_disp_width_counts_cjk_as_two():
    assert disp_width("后台 2") == 6
    assert disp_width("ab") == 2


def test_too_wide_is_clipped_and_keeps_first_task():
    mgr = _Mgr(
        [
            _bt("alice-long-name", steps=8, activity="Grep"),
            _bt("bob", tid="task_22222222"),
        ]
    )
    text = build_status_text(mgr, now=NOW, width=30)
    assert text is not None
    assert text.endswith("…")
    assert disp_width(text) <= 30
    assert "后台 2" in text
    assert "alice" in text  # ★ 第一项仍在


def test_pure_function_is_repeatable_and_non_mutating():
    """同样输入多次调用结果一致；不修改传入对象。"""
    task = _bt(steps=3, activity="Grep")
    before = (task.tool_count, task.last_activity, task.status, task.end_time)
    mgr = _Mgr([task])
    first = build_status_text(mgr, now=NOW, width=80)
    second = build_status_text(mgr, now=NOW, width=80)
    assert first == second
    assert (task.tool_count, task.last_activity, task.status, task.end_time) == before


# ── 2. 真实渲染管线 ────────────────────────────────────────────────
#
# ⚠️ 这里刻意**不断言渲染产物里有没有那行字**。`bottom_toolbar` 的最终绘制由
# prompt_toolkit 的 Renderer 决定，而它在无控制台环境下会走"单行收尾"路径：
# 同一段代码在 `python -c` 里会把 toolbar 画出来，在 pytest 里就不画
# （已实测：输出只剩 `'> hi\r\n'`）。那是第三方渲染器的环境行为，不是我们的契约。
#
# 我们**自己的**契约是：把状态文本交给 prompt_toolkit 的 bottom_toolbar，
# 且它随状态变化。下面用记录器断言这条链路 —— 稳定、且真能抓到"没接上"的回归。


async def _render(toolbar) -> str:
    """真跑一遍 prompt_toolkit 渲染管线，返回渲染产物（纯文本）。"""
    buf = io.StringIO()
    with (
        create_pipe_input() as inp,
        create_app_session(input=inp, output=PlainTextOutput(buf)),
    ):
        session = PromptSession()
        inp.send_text("hi\n")
        text = await session.prompt_async("> ", bottom_toolbar=toolbar)
    assert text == "hi"  # 管线确实跑通了
    return buf.getvalue()


class _RecordingToolbar:
    """包一层 toolbar，记录渲染器每次询问时它返回了什么。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.texts: list[str] = []

    def __call__(self) -> str:
        text = self._inner()
        self.texts.append(text)
        return text


@pytest.mark.asyncio
async def test_status_line_is_handed_to_bottom_toolbar():
    """★ 状态行**真的**经 bottom_toolbar 交给了渲染器。"""
    mgr = _Mgr([_bt(steps=8, activity="Grep")])
    tb = _RecordingToolbar(lambda: build_status_text(mgr, now=NOW, width=90))
    await _render(tb)

    assert tb.texts, "渲染器从未询问 bottom_toolbar"
    assert "后台 1" in tb.texts[0]
    assert "alice" in tb.texts[0]
    assert "8步" in tb.texts[0]


@pytest.mark.asyncio
async def test_status_line_absent_when_no_running_task():
    """★ 无运行中任务时交给渲染器的状态行为空/None（零回归）。

    `build_status_text` 在无运行中任务时返回 `None` —— 提示符下方不占行。
    """
    mgr = _Mgr()
    tb = _RecordingToolbar(lambda: build_status_text(mgr, now=NOW, width=90))
    await _render(tb)

    assert tb.texts, "渲染器从未询问 bottom_toolbar"
    assert not any(tb.texts), f"无任务时不该有状态行，得到 {tb.texts!r}"


@pytest.mark.asyncio
async def test_status_line_follows_state_change_on_rerender():
    """状态变化后再次交给渲染器，文本跟着变（证明刷新链路成立）。"""
    mgr = _Mgr([_bt(steps=8, activity="Grep")])
    tb = _RecordingToolbar(lambda: build_status_text(mgr, now=NOW, width=90))

    await _render(tb)
    assert "后台 1" in (tb.texts[-1] or "")

    mgr.tasks = [_bt(status=TaskStatus.COMPLETED, steps=8, activity="Grep")]
    await _render(tb)
    assert not tb.texts[-1], f"任务已结束仍显示状态行：{tb.texts[-1]!r}"


# ── 3. 刷新协程 ────────────────────────────────────────────────────


class _FakePromptApp:
    def __init__(self) -> None:
        self.is_running = True
        self.invalidates = 0

    def invalidate(self) -> None:
        self.invalidates += 1


class _FakeApp:
    def __init__(self, mgr: _Mgr, prompt_app: _FakePromptApp | None) -> None:
        self.task_mgr = mgr
        self.session = type("S", (), {"app": prompt_app})()


@pytest.mark.asyncio
async def test_ticker_only_invalidates_on_text_change():
    """★ 只在文本变化时重绘：没有后台任务 → **零重绘**（不引入常驻开销）。"""
    prompt_app = _FakePromptApp()
    mgr = _Mgr()
    app = _FakeApp(mgr, prompt_app)

    ticker = asyncio.create_task(_refresh_status_bar(app))
    try:
        await asyncio.sleep(_STATUS_TICK * 2.2)
        assert prompt_app.invalidates == 0, "无运行中任务时不该触发重绘"

        mgr.tasks = [_bt(steps=1)]
        await asyncio.sleep(_STATUS_TICK * 2.2)
        after_appear = prompt_app.invalidates
        assert after_appear >= 1, "出现运行中任务后应触发重绘"

        mgr.tasks = [_bt(status=TaskStatus.COMPLETED, steps=1)]
        await asyncio.sleep(_STATUS_TICK * 2.2)
        assert prompt_app.invalidates >= after_appear + 1, "状态行消失时应再重绘一次"
    finally:
        ticker.cancel()


@pytest.mark.asyncio
async def test_ticker_survives_missing_prompt_app():
    """提示符未活跃 / 无 session 时不抛异常（只是不重绘）。"""
    mgr = _Mgr([_bt(steps=1)])
    app = _FakeApp(mgr, None)

    ticker = asyncio.create_task(_refresh_status_bar(app))
    try:
        await asyncio.sleep(_STATUS_TICK * 2.2)  # 不该抛
    finally:
        ticker.cancel()


@pytest.mark.asyncio
async def test_ticker_skips_when_prompt_not_running():
    prompt_app = _FakePromptApp()
    prompt_app.is_running = False
    app = _FakeApp(_Mgr([_bt(steps=1)]), prompt_app)

    ticker = asyncio.create_task(_refresh_status_bar(app))
    try:
        await asyncio.sleep(_STATUS_TICK * 2.2)
        assert prompt_app.invalidates == 0
    finally:
        ticker.cancel()
