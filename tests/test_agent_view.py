"""`tui/agent_view.py` 粘合层单测：排队、投递、停、说话。

不碰 prompt_toolkit —— 粘合层只依赖 app 上的 `task_mgr` 与 `pending_agent_messages`，
用假 app 就能覆盖真实语义（引擎用真 `BackgroundTaskManager` + fake `run_to_completion`）。
"""

from __future__ import annotations

import asyncio

import pytest

import core.agent.sub_agent as sub_agent_mod
from conversation.manager import ConversationManager
from core.task.manager import BackgroundTask, BackgroundTaskManager, TaskStatus
from tui import agent_view


class _Agent:
    def __init__(self) -> None:
        self._test_result = "done"


class _App:
    """假 app：只提供粘合层需要的两样东西。"""

    def __init__(self, mgr: BackgroundTaskManager | None) -> None:
        self.task_mgr = mgr
        self.pending_agent_messages: dict[str, list[str]] = {}


@pytest.fixture
def mgr() -> BackgroundTaskManager:
    return BackgroundTaskManager()


@pytest.fixture
def app(mgr: BackgroundTaskManager) -> _App:
    return _App(mgr)


@pytest.fixture
def fake_run(monkeypatch):
    """把真 `run_to_completion` 换成即时返回的 fake（不联网、不花钱）。"""

    async def _fake(agent, conv, task="", events=None):
        if task:
            conv.add_user_message(task)
        return "ok"

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _fake)
    return _fake


def _register(
    mgr: BackgroundTaskManager,
    *,
    tid: str = "task_test0001",
    name: str = "alice",
    status: TaskStatus = TaskStatus.RUNNING,
    conv: ConversationManager | None = None,
    task: str = "做事",
) -> BackgroundTask:
    """手工登记一个任务（不真跑）。"""
    bt = BackgroundTask(
        id=tid,
        sub_agent=_Agent(),
        conv=conv if conv is not None else ConversationManager(),
        name=name,
        task=task,
        status=status,
    )
    if status != TaskStatus.RUNNING:
        bt.end_time = bt.start_time + 3
    mgr._tasks[tid] = bt
    if name:
        mgr._by_name[name] = tid
    return bt


# ── 列表 / 下钻 ───────────────────────────────────────────────────


def test_list_lines_without_manager():
    assert agent_view.list_lines(_App(None)) == ["没有后台任务。"]


def test_list_lines_reports_queued_count(app, mgr):
    _register(mgr, name="alice")
    app.pending_agent_messages["task_test0001"] = ["第一句"]
    body = "\n".join(agent_view.list_lines(app))
    assert "alice" in body
    assert "排队 1 条" in body


def test_show_lines_renders_transcript(app, mgr):
    conv = ConversationManager()
    conv.add_user_message("按顺序做三步")
    _register(mgr, conv=conv)
    body = "\n".join(agent_view.show_lines(app, "1"))
    assert "alice" in body
    assert "You     : 按顺序做三步" in body


def test_show_lines_bad_selector_returns_error_line(app, mgr):
    _register(mgr)
    lines = agent_view.show_lines(app, "ghost")
    assert len(lines) == 1
    assert lines[0].startswith("x ")
    assert "未找到任务" in lines[0]


def test_show_lines_accepts_id_prefix(app, mgr):
    _register(mgr, tid="task_abcdef12")
    assert "alice" in "\n".join(agent_view.show_lines(app, "task_abcd"))


# ── 说话 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tell_on_running_queues_and_does_not_touch_engine(app, mgr):
    bt = _register(mgr)
    before = len(bt.conv.messages)

    reply = await agent_view.tell(app, "1", "先跑测试再写代码")

    assert "已排队" in reply
    assert app.pending_agent_messages[bt.id] == ["先跑测试再写代码"]
    assert bt.status == TaskStatus.RUNNING
    assert len(bt.conv.messages) == before  # 没往会话里塞东西


@pytest.mark.asyncio
async def test_tell_on_stopped_resumes_immediately(app, mgr, fake_run):
    bt = _register(mgr, status=TaskStatus.COMPLETED)

    reply = await agent_view.tell(app, "alice", "接着做第二件事")
    await asyncio.sleep(0.02)  # 让 fake _runner 跑完

    assert "已续派" in reply
    assert any("接着做第二件事" in str(m.content) for m in bt.conv.messages)
    assert bt.id not in app.pending_agent_messages


@pytest.mark.asyncio
async def test_tell_on_failed_resumes(app, mgr, fake_run):
    """★ 状态放宽：失败的任务也能接着说一句（spec §3.5）。"""
    bt = _register(mgr, status=TaskStatus.FAILED)
    reply = await agent_view.tell(app, "1", "重试一次")
    await asyncio.sleep(0.02)
    assert "已续派" in reply
    assert bt.status == TaskStatus.COMPLETED  # fake 跑完 → 完成


@pytest.mark.asyncio
async def test_tell_unknown_selector(app, mgr):
    _register(mgr)
    reply = await agent_view.tell(app, "9", "hi")
    assert reply.startswith("x ")
    assert "超出范围" in reply


@pytest.mark.asyncio
async def test_tell_without_manager():
    reply = await agent_view.tell(_App(None), "1", "hi")
    assert reply.startswith("x ")


# ── 停 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_cancels_running_task(app, mgr):
    async def _slow(agent, conv, task="", events=None):
        await asyncio.sleep(30)

    import core.agent.sub_agent as mod

    orig = mod.run_to_completion
    mod.run_to_completion = _slow
    try:
        tid = await mgr.launch(_Agent(), ConversationManager(), "alice", "长任务")
        await asyncio.sleep(0)  # 让 _runner 起来
        reply = await agent_view.stop(app, "1")
        await asyncio.sleep(0.05)
        assert "已发出停止请求" in reply
        assert mgr.get(tid).status == TaskStatus.CANCELLED
    finally:
        mod.run_to_completion = orig


@pytest.mark.asyncio
async def test_stop_drops_queued_messages(app, mgr):
    bt = _register(mgr)
    app.pending_agent_messages[bt.id] = ["a", "b"]

    reply = await agent_view.stop(app, "1")

    assert "丢弃排队的 2 条消息" in reply
    assert app.pending_agent_messages == {}


@pytest.mark.asyncio
async def test_stop_unknown_selector(app, mgr):
    _register(mgr)
    assert (await agent_view.stop(app, "ghost")).startswith("x ")


@pytest.mark.asyncio
async def test_stop_without_manager():
    assert (await agent_view.stop(_App(None), "1")).startswith("x ")


# ── 排队消息的投递（任务落下时）──────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_pending_none_when_nothing_queued(app, mgr):
    _register(mgr)
    assert await agent_view.deliver_pending(app, "task_test0001") is None


@pytest.mark.asyncio
async def test_deliver_pending_merges_into_one_resume(app, mgr, fake_run):
    bt = _register(mgr, status=TaskStatus.COMPLETED)
    app.pending_agent_messages[bt.id] = ["第一句", "第二句"]

    note = await agent_view.deliver_pending(app, bt.id)
    await asyncio.sleep(0.02)

    assert note is not None and "2 条" in note
    user_msgs = [
        m for m in bt.conv.messages if str(getattr(m.role, "value", "")) == "user"
    ]
    # 两条排队消息**合并成一条**续派 —— 不为一个任务连开两轮
    assert len(user_msgs) == 1
    assert "第一句" in str(user_msgs[0].content)
    assert "第二句" in str(user_msgs[0].content)
    assert app.pending_agent_messages == {}


@pytest.mark.asyncio
async def test_deliver_pending_discards_on_failure(app, mgr):
    bt = _register(mgr, status=TaskStatus.FAILED)
    app.pending_agent_messages[bt.id] = ["白说了"]
    before = len(bt.conv.messages)

    note = await agent_view.deliver_pending(app, bt.id)

    assert note is not None and note.startswith("x ")
    assert "未投递" in note
    assert bt.status == TaskStatus.FAILED  # 没被复活
    assert len(bt.conv.messages) == before
    assert app.pending_agent_messages == {}


@pytest.mark.asyncio
async def test_deliver_pending_missing_task(app, mgr):
    app.pending_agent_messages["task_gone"] = ["hi"]
    note = await agent_view.deliver_pending(app, "task_gone")
    assert note is not None and "未投递" in note
