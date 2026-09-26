"""`_consume_task_done` 与"排队消息投递"的接线测试。

为什么单独测这一处：投递的**顺序**是要紧的 —— 必须先按第一轮结果组装
`<task-notification>`，**再**投递续派。反过来的话，续派会把 `bt.result` 清空、
状态置回 RUNNING，主 Agent 收到的通知里就没有结果了。
（见 `docs/spec_teammate_inspect.md` §3.4）
"""

from __future__ import annotations

import asyncio
import io

import pytest
from rich.console import Console

import core.agent.sub_agent as sub_agent_mod
import tui.app as app_mod
from conversation.manager import ConversationManager
from core.task.manager import BackgroundTask, BackgroundTaskManager, TaskStatus


class _Agent:
    def __init__(self) -> None:
        self._test_result = "done"


class _Runtime:
    def __init__(self) -> None:
        self.pending_reminders: list[str] = []


class _App:
    """`_consume_task_done` 需要的最小 app 面。"""

    def __init__(self, mgr: BackgroundTaskManager) -> None:
        self.task_mgr = mgr
        self.runtime = _Runtime()
        self.conversation = ConversationManager()
        self.console = Console(file=io.StringIO(), width=200, no_color=True)
        self.agent_running = True  # 跳过"主动唤醒 Lead"那条支路
        self.pending_agent_messages: dict[str, list[str]] = {}


@pytest.fixture
def fake_run(monkeypatch):
    async def _fake(agent, conv, task="", events=None):
        if task:
            conv.add_user_message(task)
        return "ok"

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _fake)


@pytest.fixture
def no_wake(monkeypatch):
    """记录（而不是真跑）`_inject_and_run`。"""

    calls: list[str] = []

    async def _fake(app, text):
        calls.append(text)

    monkeypatch.setattr(app_mod, "_inject_and_run", _fake)
    return calls


def _register(
    mgr: BackgroundTaskManager,
    *,
    tid: str = "task_notify01",
    status: TaskStatus = TaskStatus.COMPLETED,
    result: str = "第一轮产出",
) -> BackgroundTask:
    bt = BackgroundTask(
        id=tid,
        sub_agent=_Agent(),
        conv=ConversationManager(),
        name="alice",
        task="做事",
        status=status,
    )
    bt.result = result
    mgr._tasks[tid] = bt
    return bt


async def _run_one_tick(app, task_id: str) -> None:
    """跑消费协程，处理完一条完成通知后收工。"""
    consumer = asyncio.create_task(app_mod._consume_task_done(app))
    try:
        app.task_mgr._done.put_nowait(task_id)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if app.runtime.pending_reminders:
                return
    finally:
        consumer.cancel()


@pytest.mark.asyncio
async def test_notification_keeps_first_result_then_delivers(fake_run, no_wake):
    """★ 通知里必须是**第一轮**的结果，同时排队消息真的被续派。"""
    mgr = BackgroundTaskManager()
    bt = _register(mgr)
    app = _App(mgr)
    app.pending_agent_messages[bt.id] = ["接着做第二件事"]

    await _run_one_tick(app, bt.id)
    await asyncio.sleep(0.05)

    assert len(app.runtime.pending_reminders) == 1
    note = app.runtime.pending_reminders[0]
    assert "第一轮产出" in note  # 结果没被续派清掉
    assert "completed" in note
    assert "已把排队的 1 条消息续派给" in note
    # 真的续派并跑了一轮（fake run_to_completion 会立刻把第二轮跑完，
    # 所以状态又回到 COMPLETED —— 这里断言的是"确实发生了第二轮"）
    assert any("接着做第二件事" in str(m.content) for m in bt.conv.messages)
    assert bt.result == "ok"
    assert app.pending_agent_messages == {}


@pytest.mark.asyncio
async def test_notification_without_pending_is_unchanged(fake_run, no_wake):
    """没有排队消息时，通知与引入本功能之前**逐字一致**。"""
    mgr = BackgroundTaskManager()
    bt = _register(mgr)
    app = _App(mgr)

    await _run_one_tick(app, bt.id)

    note = app.runtime.pending_reminders[0]
    assert note == (
        "<task-notification>\n"
        f'Task {bt.id} (name="alice"): completed\n'
        "Result: 第一轮产出\n"
        "</task-notification>"
    )
    assert bt.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_task_discards_pending_and_says_so(fake_run, no_wake):
    mgr = BackgroundTaskManager()
    bt = _register(mgr, status=TaskStatus.FAILED, result="[failed] boom")
    app = _App(mgr)
    app.pending_agent_messages[bt.id] = ["排了但跑挂了"]

    await _run_one_tick(app, bt.id)

    note = app.runtime.pending_reminders[0]
    assert "未投递" in note
    assert bt.status == TaskStatus.FAILED
    assert app.pending_agent_messages == {}
