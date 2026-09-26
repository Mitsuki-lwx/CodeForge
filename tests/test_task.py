"""后台任务管理器 单元测试。

覆盖：launch / 完成状态 / 失败状态 / stop / send_message / done 队列 / 4 个管理工具。
通过 monkeypatch 替换 core.agent.sub_agent.run_to_completion 为 fake，
避免构造完整 Agent。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import core.agent.sub_agent as sub_agent_mod
from conversation.manager import ConversationManager
from core.task.manager import (
    BackgroundTask,
    BackgroundTaskManager,
    TaskBusyError,
    TaskNotFoundError,
    TaskStatus,
)
from core.task.tools import (
    SendMessageTool,
    TaskGetTool,
    TaskListTool,
    TaskStopTool,
)
from core.tool.context import ExecutionContext


@pytest.fixture
def manager() -> BackgroundTaskManager:
    return BackgroundTaskManager()


class _Calls:
    """fake_run 的调用记录。"""

    def __init__(self) -> None:
        self.results = []
        self.events_seen: list[tuple] = []
        self.booms = []


@pytest.fixture
def fake_run():
    """monkeypatch run_to_completion 为可编程 fake。"""

    async def _fake(agent, conv, task="", events=None):
        calls = agent._test_calls
        if task:
            conv.add_user_message(task)
        if getattr(agent, "_boom", False):
            raise RuntimeError("boom")
        # 模拟工具事件
        if events is not None:
            try:
                events.put_nowait(("tool", "fake_tool"))
                events.put_nowait(("tool", "bash"))
            except asyncio.QueueFull:
                pass
        result = agent._test_result
        calls.results.append(result)
        return result

    return _fake


def _make_agent(result: str = "done", calls=None) -> object:
    class _Agent:
        def __init__(self) -> None:
            self._test_calls = calls if calls is not None else _Calls()
            self._test_result = result
            self._boom = False

    return _Agent()


def _ctx() -> ExecutionContext:
    return ExecutionContext(cwd=Path.cwd(), session_id="test")


def _register_task(
    mgr: BackgroundTaskManager,
    tid: str,
    name: str,
    status: TaskStatus,
    *,
    conv: ConversationManager | None = None,
) -> BackgroundTask:
    """手工登记一个任务（不进 `launch`，不真跑）。"""
    bt = BackgroundTask(
        id=tid,
        name=name,
        sub_agent=_make_agent(),
        conv=conv if conv is not None else ConversationManager(),
        status=status,
    )
    mgr._tasks[tid] = bt
    if name:
        mgr._by_name[name] = tid
    return bt


# ── launch 与状态 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_launch_completed(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    conv = ConversationManager()
    agent = _make_agent("final output")

    task_id = await mgr.launch(agent, conv, "worker", "do it")
    assert task_id.startswith("task_")

    q = mgr.subscribe_done()
    done_id = await asyncio.wait_for(q.get(), timeout=3)
    assert done_id == task_id

    bt = mgr.get(task_id)
    assert bt is not None
    assert bt.status == TaskStatus.COMPLETED
    assert bt.result == "final output"


@pytest.mark.asyncio
async def test_launch_failed(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    conv = ConversationManager()
    agent = _make_agent()
    agent._boom = True

    task_id = await mgr.launch(agent, conv, "", "do it")
    q = mgr.subscribe_done()
    done_id = await asyncio.wait_for(q.get(), timeout=3)
    assert done_id == task_id

    bt = mgr.get(task_id)
    assert bt.status == TaskStatus.FAILED
    assert bt.err is not None
    assert "boom" in str(bt.err)


@pytest.mark.asyncio
async def test_launch_aggregates_tool_count(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    conv = ConversationManager()
    agent = _make_agent("res")

    task_id = await mgr.launch(agent, conv, "w", "task")
    q = mgr.subscribe_done()
    await asyncio.wait_for(q.get(), timeout=3)

    bt = mgr.get(task_id)
    assert bt.tool_count == 2  # 2 个模拟工具事件
    assert bt.last_activity == "bash"


@pytest.mark.asyncio
async def test_tool_count_visible_while_running(monkeypatch):
    """★ **运行期间**就能读到 tool_count / last_activity。

    修复前 `_aggregate_events` 只在收尾被调用一次 ⇒ 运行中恒为 0/""，
    而 `core/task/tools.py` 把这两个字段**给模型看**（TaskList / TaskGet）——
    模型据此判断"任务在不在动"，拿到的一直是空值。
    见 docs/spec_teammate_status.md §1.3。
    """
    release = asyncio.Event()

    async def _slow(agent, conv, task="", events=None):
        if events is not None:
            events.put_nowait(("tool", "grep"))
            events.put_nowait(("tool", "read_file"))
        await release.wait()  # 卡住：让断言发生在"运行中"这一时刻
        return "done"

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _slow)
    mgr = BackgroundTaskManager()
    task_id = await mgr.launch(_make_agent("res"), ConversationManager(), "w", "task")
    bt = mgr.get(task_id)
    assert bt is not None

    for _ in range(60):  # 等聚合协程至少跑一拍（间隔 0.4s）
        await asyncio.sleep(0.05)
        if bt.tool_count:
            break

    assert bt.status == TaskStatus.RUNNING, "断言必须发生在任务结束前"
    assert bt.tool_count == 2, f"运行中 tool_count 应为 2，实际 {bt.tool_count}"
    assert bt.last_activity == "read_file"

    release.set()
    await asyncio.wait_for(mgr.subscribe_done().get(), timeout=3)
    assert bt.status == TaskStatus.COMPLETED
    assert bt.tool_count == 2, "收尾后计数不应丢"


@pytest.mark.asyncio
async def test_events_beyond_queue_capacity_not_lost(monkeypatch):
    """★ 持续产生的事件不再被队列容量(64)截断。

    修复前没人边跑边消费：队列塞满 64 后生产侧
    `except asyncio.QueueFull: pass` **静默丢弃**（流的 text 事件还会先占满队列，
    把后来的工具事件挤掉），连最终 `tool_count` 都是被截断的。
    """
    total = 100  # > 队列 maxsize(64)

    async def _flood(agent, conv, task="", events=None):
        for i in range(total):
            try:
                events.put_nowait(("tool", f"tool{i}"))
            except asyncio.QueueFull:
                pass  # 与生产侧同构：满了就丢
            await asyncio.sleep(0.01)  # 拉开 ~1s，覆盖多个聚合周期
        return "done"

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _flood)
    mgr = BackgroundTaskManager()
    task_id = await mgr.launch(_make_agent("res"), ConversationManager(), "w", "task")
    await asyncio.wait_for(mgr.subscribe_done().get(), timeout=20)

    bt = mgr.get(task_id)
    assert bt is not None
    assert bt.tool_count == total, f"应计入全部 {total} 个事件，实际 {bt.tool_count}"


@pytest.mark.asyncio
async def test_task_list_tool_exposes_live_progress(monkeypatch):
    """模型侧（TaskList）在**运行中**就能看到进展 —— 这是上面修复的目的。"""
    release = asyncio.Event()

    async def _slow(agent, conv, task="", events=None):
        if events is not None:
            events.put_nowait(("tool", "grep"))
        await release.wait()
        return "done"

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _slow)
    mgr = BackgroundTaskManager()
    await mgr.launch(_make_agent("res"), ConversationManager(), "worker", "task")
    tool = TaskListTool(mgr)

    data = ""
    for _ in range(60):
        await asyncio.sleep(0.05)
        result = await tool.execute(_ctx(), {})
        data = result.data
        if '"tool_count": 1' in data:
            break

    assert '"status": "running"' in data
    assert '"tool_count": 1' in data, f"运行中应已可见计数，实际：{data[:200]}"
    assert '"last_activity": "grep"' in data

    release.set()
    await asyncio.wait_for(mgr.subscribe_done().get(), timeout=3)


@pytest.mark.asyncio
async def test_stop_nonexistent(manager):
    ok = await manager.stop("nonexistent")
    assert ok is False


@pytest.mark.asyncio
async def test_stop_cancels(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    conv = ConversationManager()
    agent = _make_agent("x")

    task_id = await mgr.launch(agent, conv, "", "t")
    bt = mgr.get(task_id)
    assert bt is not None
    ok = await mgr.stop(task_id)
    assert ok is True


@pytest.mark.asyncio
async def test_list_returns_sorted(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    await mgr.launch(_make_agent("a"), ConversationManager(), "", "1")
    await mgr.launch(_make_agent("b"), ConversationManager(), "", "2")
    tasks = mgr.list()
    assert len(tasks) == 2
    assert tasks[0].start_time <= tasks[1].start_time


@pytest.mark.asyncio
async def test_send_message_resumes_completed(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    conv = ConversationManager()
    agent = _make_agent("first")

    task_id = await mgr.launch(agent, conv, "worker", "task one")
    q = mgr.subscribe_done()
    await asyncio.wait_for(q.get(), timeout=3)
    bt = mgr.get(task_id)
    assert bt.status == TaskStatus.COMPLETED

    # 续派
    new_id = await mgr.send_message("worker", "task two")
    assert new_id == task_id
    assert mgr.get(task_id).status == TaskStatus.RUNNING

    # 等待第二次完成
    await asyncio.wait_for(q.get(), timeout=3)
    assert mgr.get(task_id).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_send_message_not_found(manager):
    with pytest.raises(TaskNotFoundError):
        await manager.send_message("ghost", "hello")


# ── send_message_to：按 id 续派（spec_teammate_inspect §3.5）────────


@pytest.mark.asyncio
async def test_send_message_to_unnamed_task_by_id(monkeypatch, fake_run):
    """★ 未命名任务也能续派 —— 这是加 `send_message_to` 的直接动机。"""
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    conv = ConversationManager()

    task_id = await mgr.launch(_make_agent("first"), conv, "", "task one")
    q = mgr.subscribe_done()
    await asyncio.wait_for(q.get(), timeout=3)
    assert mgr.get(task_id).status == TaskStatus.COMPLETED

    same_id = await mgr.send_message_to(task_id, "task two")
    assert same_id == task_id
    assert mgr.get(task_id).status == TaskStatus.RUNNING

    await asyncio.wait_for(q.get(), timeout=3)
    assert mgr.get(task_id).status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_send_message_to_running_raises():
    """RUNNING 仍必须拒绝：不能对并发中的会话再写一条 user 消息。"""
    mgr = BackgroundTaskManager()
    _register_task(mgr, "task_busy2", "busy2", TaskStatus.RUNNING)
    with pytest.raises(TaskBusyError):
        await mgr.send_message_to("task_busy2", "hi")


@pytest.mark.asyncio
async def test_send_message_to_unknown_id():
    mgr = BackgroundTaskManager()
    with pytest.raises(TaskNotFoundError):
        await mgr.send_message_to("task_ghost", "hi")


@pytest.mark.asyncio
async def test_send_message_to_allows_failed(monkeypatch, fake_run):
    """★ 状态放宽：失败的任务也能"接着说一句"。"""
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    _register_task(mgr, "task_failed1", "f1", TaskStatus.FAILED, conv=ConversationManager())

    await mgr.send_message_to("task_failed1", "重试一次")
    assert mgr.get("task_failed1").status == TaskStatus.RUNNING

    q = mgr.subscribe_done()
    await asyncio.wait_for(q.get(), timeout=3)
    assert mgr.get("task_failed1").status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_send_message_to_resets_progress(monkeypatch, fake_run):
    """续派要把上一轮的产物清干净，否则状态行/列表会显示陈旧步数。"""
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    bt = _register_task(mgr, "task_reset", "r", TaskStatus.COMPLETED, conv=ConversationManager())
    bt.tool_count = 9
    bt.last_activity = "Grep"
    bt.result = "old"

    await mgr.send_message_to("task_reset", "再来")

    assert bt.tool_count == 0
    assert bt.last_activity == ""
    assert bt.result == ""


@pytest.mark.asyncio
async def test_send_message_busy():
    """send_message 给 RUNNING 任务 → TaskBusyError。"""
    mgr = BackgroundTaskManager()
    conv = ConversationManager()
    agent = _make_agent("x")

    # 手动注册一个 RUNNING 任务（不真正跑）
    bt = BackgroundTask(
        id="task_busy",
        name="busy",
        sub_agent=agent,
        conv=conv,
        status=TaskStatus.RUNNING,
    )
    mgr._tasks["task_busy"] = bt
    mgr._by_name["busy"] = "task_busy"

    with pytest.raises(TaskBusyError):
        await mgr.send_message("busy", "hi")


@pytest.mark.asyncio
async def test_cancel_all(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    await mgr.launch(_make_agent("a"), ConversationManager(), "", "1")
    await mgr.launch(_make_agent("b"), ConversationManager(), "", "2")
    await mgr.cancel_all()
    # 不抛异常即可
    assert len(mgr.list()) == 2


# ── 4 个管理工具 ───────────────────────────────────────────────────


def test_task_tool_names(manager):
    assert TaskListTool(manager).name() == "TaskList"
    assert TaskGetTool(manager).name() == "TaskGet"
    assert TaskStopTool(manager).name() == "TaskStop"
    assert SendMessageTool(manager).name() == "SendMessage"


@pytest.mark.asyncio
async def test_task_list_tool(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    await mgr.launch(_make_agent("a"), ConversationManager(), "w1", "do 1")
    tool = TaskListTool(mgr)
    result = await tool.execute(_ctx(), {})
    assert result.success
    assert "task_" in result.data
    assert "w1" in result.data


@pytest.mark.asyncio
async def test_task_get_tool(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    task_id = await mgr.launch(_make_agent("x"), ConversationManager(), "w", "do")
    tool = TaskGetTool(mgr)
    result = await tool.execute(_ctx(), {"task_id": task_id})
    assert result.success
    assert task_id in result.data


@pytest.mark.asyncio
async def test_task_get_tool_not_found(manager):
    tool = TaskGetTool(manager)
    result = await tool.execute(_ctx(), {"task_id": "nonexistent"})
    assert not result.success


@pytest.mark.asyncio
async def test_task_get_schema(manager):
    tool = TaskGetTool(manager)
    schema = tool.input_schema()
    assert "task_id" in schema["properties"]
    assert "task_id" in schema["required"]


@pytest.mark.asyncio
async def test_task_stop_tool(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    task_id = await mgr.launch(_make_agent("x"), ConversationManager(), "", "do")
    tool = TaskStopTool(mgr)
    result = await tool.execute(_ctx(), {"task_id": task_id})
    assert result.success
    assert "cancellation_requested" in result.data


@pytest.mark.asyncio
async def test_send_message_tool(monkeypatch, fake_run):
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    conv = ConversationManager()
    await mgr.launch(_make_agent("first"), conv, "w", "do one")
    q = mgr.subscribe_done()
    await asyncio.wait_for(q.get(), timeout=3)
    tool = SendMessageTool(mgr)
    result = await tool.execute(_ctx(), {"name": "w", "message": "follow up"})
    assert result.success
    assert "resumed" in result.data


@pytest.mark.asyncio
async def test_send_message_tool_not_found(manager):
    tool = SendMessageTool(manager)
    result = await tool.execute(_ctx(), {"name": "ghost", "message": "hi"})
    assert not result.success


# ── §1.9 / §1.10 / §3 补测（此前无人断言）─────────────────────────


@pytest.mark.asyncio
async def test_by_name_latest_launch_wins(monkeypatch, fake_run):
    """同名任务：后启动者覆盖 `_by_name`（§1.9）。

    覆盖是「弱引用」语义——前一个任务仍在 `list()` 里，只是按名字再也找不到它。
    """
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()
    q = mgr.subscribe_done()

    first = await mgr.launch(_make_agent("a"), ConversationManager(), "dup", "one")
    await asyncio.wait_for(q.get(), timeout=3)
    second = await mgr.launch(_make_agent("b"), ConversationManager(), "dup", "two")
    await asyncio.wait_for(q.get(), timeout=3)

    assert first != second
    assert mgr._by_name["dup"] == second, "后启动的必须覆盖前一个"

    # 行为面：按名字寻址打到的是第二个（SendMessage 走的就是 _by_name）
    assert await mgr.send_message("dup", "again") == second


@pytest.mark.asyncio
async def test_done_queue_full_drops_notification_with_warning(
    monkeypatch, fake_run, capsys
):
    """done 队列满（32）→ QueueFull 被捕获 + stderr 警告（§3）。

    反证：通知是**真的被丢掉了**（队列里仍是那 32 条占位），而不是静默塞进去了。
    """
    monkeypatch.setattr(sub_agent_mod, "run_to_completion", fake_run)
    mgr = BackgroundTaskManager()

    for i in range(32):
        mgr._done.put_nowait(f"filler_{i}")
    assert mgr._done.full()

    task_id = await mgr.launch(_make_agent("r"), ConversationManager(), "", "x")
    await asyncio.wait_for(mgr.get(task_id).handle, timeout=3)

    err = capsys.readouterr().err
    assert "done queue full" in err, f"应有 stderr 警告，实际 {err!r}"

    drained = [mgr._done.get_nowait() for _ in range(mgr._done.qsize())]
    assert drained == [f"filler_{i}" for i in range(32)], "队列内容不该被改动"
    assert task_id not in drained, "满队列时通知必须被丢弃，不能挤进去"


def test_task_tools_are_system_tools(manager):
    """四个管理工具都标了 `is_system_tool = True`（§1.10）。

    这面旗子决定它们是否被当作"元工具"排除在子 Agent 工具集之外；
    此前代码里设了，但没有任何测试守着。
    """
    for cls in (TaskListTool, TaskGetTool, TaskStopTool, SendMessageTool):
        assert cls(manager).is_system_tool is True, f"{cls.__name__} 未标 system tool"


# ── stop：取消必须留下终态（spec_teammate_inspect §10）──────────────


@pytest.mark.asyncio
async def test_stop_never_started_task_finalizes(monkeypatch):
    """★ 派出去**立刻**停：协程从未被调度，`_runner` 不会执行。

    实测过：`cancel()` 只把 `CancelledError` 丢在协程起点，函数体一行都不跑，
    于是终态/聚合/完成通知全都没有 —— 任务永远停在 RUNNING。
    """
    import inspect

    async def _slow(agent, conv, task="", events=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _slow)

    mgr = BackgroundTaskManager()
    fired: list[str] = []

    async def _cb(task_id: str) -> None:
        fired.append(task_id)

    mgr.on_task_done(_cb)
    q = mgr.subscribe_done()

    task_id = await mgr.launch(_make_agent(), ConversationManager(), "alice", "做事")
    handle = mgr.get(task_id).handle
    # 前置条件自检：这一刻协程确实**还没被调度**（否则本用例什么都没测到）
    assert inspect.getcoroutinestate(handle.get_coro()) == "CORO_CREATED"

    assert await mgr.stop(task_id) is True
    await asyncio.sleep(0.05)

    bt = mgr.get(task_id)
    assert bt.status == TaskStatus.CANCELLED
    assert bt.result == "[cancelled]"
    assert bt.end_time > 0
    assert q.qsize() == 1 and q.get_nowait() == task_id
    assert fired == [task_id]


@pytest.mark.asyncio
async def test_stop_started_task_notifies_exactly_once(monkeypatch):
    """已启动的协程自己会收尾 —— 兜底逻辑不能让它多发一次完成通知。"""
    async def _slow(agent, conv, task="", events=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _slow)

    mgr = BackgroundTaskManager()
    q = mgr.subscribe_done()
    task_id = await mgr.launch(_make_agent(), ConversationManager(), "bob", "做事")
    await asyncio.sleep(0)  # 让它跑起来（进入 try 之后挂住）

    assert await mgr.stop(task_id) is True
    await asyncio.sleep(0.05)

    assert mgr.get(task_id).status == TaskStatus.CANCELLED
    assert q.qsize() == 1, f"完成通知应恰好一条，实际 {q.qsize()}"


@pytest.mark.asyncio
async def test_stop_unknown_and_finished(manager):
    """找不到 → False；已结束 → True 且不重复收尾。"""
    assert await manager.stop("task_ghost") is False

    mgr = BackgroundTaskManager()
    bt = _register_task(mgr, "task_done1", "d", TaskStatus.COMPLETED)
    assert await mgr.stop("task_done1") is True
    assert bt.status == TaskStatus.COMPLETED  # 没有把它改成 CANCELLED
