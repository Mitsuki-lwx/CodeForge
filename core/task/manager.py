"""后台任务管理器。

管理子 Agent 后台任务的全生命周期：创建、执行、停止、续派。
通过 asyncio.Queue 通知任务完成，TUI 消费后注入主对话。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import IntEnum

from conversation.manager import ConversationManager

logger = logging.getLogger(__name__)


class TaskStatus(IntEnum):
    """后台任务状态。"""

    RUNNING = 0
    COMPLETED = 1
    FAILED = 2
    CANCELLED = 3


@dataclass
class TaskUsage:
    """子 Agent token 用量。"""

    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0


@dataclass
class BackgroundTask:
    """一个后台子 Agent 的完整状态快照。"""

    id: str
    sub_agent: object = field(repr=False)  # Agent 实例
    conv: ConversationManager = field(repr=False)
    name: str = ""
    task: str = ""  # 初始任务文本
    status: TaskStatus = TaskStatus.RUNNING
    result: str = ""
    err: BaseException | None = field(default=None, repr=False)
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0
    handle: asyncio.Task | None = field(default=None, repr=False)
    usage: TaskUsage = field(default_factory=TaskUsage)
    tool_count: int = 0
    last_activity: str = ""


@dataclass
class PartialState:
    """前→后台移交时已收集的中间状态。"""

    last_text: str = ""
    tool_count: int = 0
    last_activity: str = ""
    usage: TaskUsage = field(default_factory=TaskUsage)


class TaskBusyError(Exception):
    """任务状态不允许当前操作（如 send_message 给 RUNNING 任务）。"""


class TaskNotFoundError(Exception):
    """任务 ID 或 name 未找到。"""


class BackgroundTaskManager:
    """管理后台任务。协程安全（单事件循环）。

    提供 launch / adopt_running / stop / send_message / send_message_to / get / list 等操作，
    通过 subscribe_done() 返回的 asyncio.Queue 通知任务完成。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._by_name: dict[str, str] = {}  # name → id（弱引用，后启动覆盖前）
        self._done: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._counter: int = 0
        self._done_callbacks: list[object] = []  # on_task_done 回调
        self._name_reg: object | None = None  # AgentNameRegistry（团队寻址用）

    # ── 团队集成（AgentNameRegistry / on_task_done）─────────────────

    def set_name_registry(self, reg: object | None) -> None:
        """注入 AgentNameRegistry；launch 有 name 时登记进注册表。"""
        self._name_reg = reg

    def on_task_done(self, fn: object) -> None:
        """注册任务完成回调（如 idle 通知）。可注册多个。"""
        self._done_callbacks.append(fn)

    async def _notify_done(self, task_id: str) -> None:
        """在任务完成 finally 里触发 on_task_done 回调（best-effort）。"""
        for fn in list(self._done_callbacks):
            try:
                await fn(task_id)
            except Exception as e:  # noqa: BLE001 —— 回调失败不拖垮任务
                logger.warning("on_task_done callback failed for %s: %s", task_id, e)

    # ── 查询 ───────────────────────────────────────────────────────

    def get(self, task_id: str) -> BackgroundTask | None:
        """按 ID 获取任务。"""
        return self._tasks.get(task_id)

    def list(self) -> list[BackgroundTask]:
        """返回当前全部任务（按 start_time 升序）。"""
        return sorted(self._tasks.values(), key=lambda bt: bt.start_time)

    def subscribe_done(self) -> asyncio.Queue[str]:
        """返回完成通知队列。消费者从中拿 task_id。"""
        return self._done

    # ── 生命周期 ───────────────────────────────────────────────────

    async def launch(
        self,
        agent: object,
        conv: ConversationManager,
        name: str = "",
        task_text: str = "",
    ) -> str:
        """启动一个后台子 Agent。

        Args:
            agent: Agent 实例。
            conv: 子对话 ConversationManager。
            name: 可选名称（供 SendMessage 查找）。
            task_text: 初始任务文本。

        Returns:
            task_id，格式 "task_<8 位 hex>"。
        """
        task_id = self._next_id()
        bt = BackgroundTask(
            id=task_id,
            name=name,
            sub_agent=agent,
            conv=conv,
            task=task_text,
            status=TaskStatus.RUNNING,
        )

        self._tasks[task_id] = bt
        if name:
            self._by_name[name] = task_id
        if name and self._name_reg is not None:
            try:
                self._name_reg.register(name, task_id)
            except Exception:  # noqa: BLE001, S110 —— 名册登记失败不影响任务启动
                pass

        # 聚合事件队列
        events: asyncio.Queue = asyncio.Queue(maxsize=64)

        async def _runner() -> None:
            # ⚠️ 不变量：`try` 之前**不得新增 `await`** —— 否则"已被调度但还没进
            # try"的窗口里收到取消，`stop()` 的 `CORO_CREATED` 判定会漏（见 stop 说明）。
            # 边跑边聚合：否则运行期间 `tool_count`/`last_activity` 恒为 0/""，
            # 且事件队列（maxsize=64）满了之后生产侧静默丢弃 → 最终计数也被截断。
            # 见 `docs/spec_teammate_status.md` §1.3。
            drainer = asyncio.create_task(_drain_events_periodically(events, bt))
            try:
                # 动态导入避免循环依赖
                from core.agent.sub_agent import run_to_completion as _rtc

                text = await _rtc(agent, conv, task_text, events)
                bt.result = text
                bt.status = TaskStatus.COMPLETED
            except asyncio.CancelledError:
                bt.status = TaskStatus.CANCELLED
                bt.result = "[cancelled]"
            except BaseException as e:  # noqa: BLE001 —— 后台任务崩溃转为 FAILED，绝不波及主程序
                bt.status = TaskStatus.FAILED
                bt.err = e
                bt.result = f"[failed] {e}"
                logger.warning("background task %s failed: %s", task_id, e)
            finally:
                # 立刻取消聚合协程，**不 `await`**：收尾路径上不能新增 await 点 ——
                # 否则 `_notify_done` 会被推迟到后续事件循环迭代才执行，
                # 破坏既有调用方 `sleep(0.01)` 量级的时间假设（实测 2 项测试因此变红）。
                # 与 `cancel_all` 一致（只 cancel，不 await）。
                # 队列已在下一行被 `_aggregate_events` 收干，晚到的 drain 不会再加计数。
                drainer.cancel()
                bt.end_time = time.monotonic()
                _aggregate_events(events, bt)
                try:
                    self._done.put_nowait(task_id)
                except asyncio.QueueFull:
                    print(
                        f"task manager: done queue full, dropping notification for {task_id}",
                        file=sys.stderr,
                    )
                await self._notify_done(task_id)

        bt.handle = asyncio.create_task(_runner())
        return task_id

    async def adopt_running(
        self,
        agent: object,
        conv: ConversationManager,
        name: str = "",
        handle: asyncio.Task | None = None,
        partial: PartialState | None = None,
    ) -> str:
        """接管已在跑的 Agent 到后台管理。

        Args:
            agent: 正在跑的 Agent 实例。
            conv: 子对话。
            name: 可选名称。
            handle: 已存在的 asyncio.Task（来自前台 asyncio.wait_for）。
            partial: 已收集的中间状态。

        Returns:
            task_id。
        """
        task_id = self._next_id()
        bt = BackgroundTask(
            id=task_id,
            name=name,
            sub_agent=agent,
            conv=conv,
            task="",
            status=TaskStatus.RUNNING,
        )

        if partial is not None:
            bt.tool_count = partial.tool_count
            bt.last_activity = partial.last_activity
            bt.usage = partial.usage

        self._tasks[task_id] = bt
        if name:
            self._by_name[name] = task_id

        if handle is not None:
            bt.handle = handle

        return task_id

    async def stop(self, task_id: str) -> bool:
        """停止一个运行中的后台任务。

        ★ `cancel()` 对**从未被事件循环调度过**的协程（`CORO_CREATED`）只是把
        `CancelledError` 丢在协程起点：`_runner` 的函数体**一行都不会执行**，
        于是"置终态 / 收尾聚合 / done 通知"三件事全都没发生 —— 任务会**永远停在
        `RUNNING`**（状态行与 `/agents` 一直显示"运行中"，无头模式则一直等它）。
        真链路复现与判定见 `docs/spec_teammate_inspect.md` §10。这里由 `stop` 兜底。

        Args:
            task_id: 任务 ID。

        Returns:
            True 如果找到并发出取消请求。
        """
        bt = self._tasks.get(task_id)
        if bt is None:
            return False
        handle = bt.handle
        if handle is None or handle.done():
            return True

        # 判定必须在 `cancel()` **之前**、且中间不能有 await：
        # 单线程事件循环里这段是原子的，"查"与"取消"之间任务不可能启动。
        never_started = _coro_state(handle) == "CORO_CREATED"
        handle.cancel()
        if never_started:
            self._finalize_never_started(bt, task_id)
            await self._notify_done(task_id)
        return True

    def _finalize_never_started(self, bt: BackgroundTask, task_id: str) -> None:
        """给"从未启动就被取消"的任务补终态（`_runner` 不会执行，只能这里补）。"""
        bt.status = TaskStatus.CANCELLED
        bt.result = "[cancelled]"
        bt.err = None
        bt.end_time = time.monotonic()
        try:
            self._done.put_nowait(task_id)
        except asyncio.QueueFull:
            print(
                f"task manager: done queue full, dropping notification for {task_id}",
                file=sys.stderr,
            )

    async def send_message(self, name: str, message: str) -> str:
        """向已停下的后台 Agent 续派新任务（按**名字**）。

        Args:
            name: 任务名称（Agent 工具 name 参数）。
            message: 新任务描述。

        Returns:
            task_id（与原来相同）。

        Raises:
            TaskNotFoundError: name 未找到。
            TaskBusyError: 任务正在跑（RUNNING）。
        """
        task_id = self._by_name.get(name)
        if task_id is None:
            raise TaskNotFoundError(f"no task with name '{name}'")
        return await self.send_message_to(task_id, message)

    async def send_message_to(self, task_id: str, message: str) -> str:
        """向已停下的后台 Agent 续派新任务（按 **task_id**）。

        与 `send_message` 的区别只有寻址方式：`AgentTool` 的 `name` 是**可选**参数，
        未命名的后台任务很常见，只有 id 才找得到它。

        非 RUNNING 即可续派（`COMPLETED` / `FAILED` / `CANCELLED`）——
        "失败了接着说一句"是合理诉求；**RUNNING 仍必须拒绝**，
        否则就是对正在跑的会话并发写 user 消息。

        Raises:
            TaskNotFoundError: task_id 未找到。
            TaskBusyError: 任务正在跑（RUNNING）。
        """
        bt = self._tasks.get(task_id)
        if bt is None:
            raise TaskNotFoundError(f"task '{task_id}' no longer exists")

        if bt.status == TaskStatus.RUNNING:
            raise TaskBusyError(
                f"task '{getattr(bt, 'name', '') or task_id}' is RUNNING, not stopped"
            )

        # 追加新 user 消息并重新启动
        bt.conv.add_user_message(message)
        bt.status = TaskStatus.RUNNING
        bt.result = ""
        bt.err = None
        bt.tool_count = 0
        bt.last_activity = ""

        events: asyncio.Queue = asyncio.Queue(maxsize=64)

        async def _runner() -> None:
            # ⚠️ 不变量：`try` 之前**不得新增 `await`** —— 否则"已被调度但还没进
            # try"的窗口里收到取消，`stop()` 的 `CORO_CREATED` 判定会漏（见 stop 说明）。
            # 边跑边聚合：否则运行期间 `tool_count`/`last_activity` 恒为 0/""，
            # 且事件队列（maxsize=64）满了之后生产侧静默丢弃 → 最终计数也被截断。
            # 见 `docs/spec_teammate_status.md` §1.3。
            drainer = asyncio.create_task(_drain_events_periodically(events, bt))
            try:
                from core.agent.sub_agent import run_to_completion as _rtc

                text = await _rtc(bt.sub_agent, bt.conv, "", events)
                bt.result = text
                bt.status = TaskStatus.COMPLETED
            except asyncio.CancelledError:
                bt.status = TaskStatus.CANCELLED
                bt.result = "[cancelled]"
            except BaseException as e:  # noqa: BLE001 —— 续派任务崩溃转 FAILED
                bt.status = TaskStatus.FAILED
                bt.err = e
                bt.result = f"[failed] {e}"
            finally:
                # 立刻取消聚合协程，**不 `await`**：收尾路径上不能新增 await 点 ——
                # 否则 `_notify_done` 会被推迟到后续事件循环迭代才执行，
                # 破坏既有调用方 `sleep(0.01)` 量级的时间假设（实测 2 项测试因此变红）。
                # 与 `cancel_all` 一致（只 cancel，不 await）。
                # 队列已在下一行被 `_aggregate_events` 收干，晚到的 drain 不会再加计数。
                drainer.cancel()
                bt.end_time = time.monotonic()
                _aggregate_events(events, bt)
                try:
                    self._done.put_nowait(task_id)
                except asyncio.QueueFull:
                    print(
                        f"task manager: done queue full, dropping notification for {task_id}",
                        file=sys.stderr,
                    )
                await self._notify_done(task_id)

        bt.handle = asyncio.create_task(_runner())
        return task_id

    async def cancel_all(self) -> None:
        """取消全部运行中的任务（父会话关闭时调用）。"""
        for bt in self._tasks.values():
            if bt.status == TaskStatus.RUNNING and bt.handle is not None:
                bt.handle.cancel()

    # ── 内部 ───────────────────────────────────────────────────────

    def _next_id(self) -> str:
        self._counter += 1
        import secrets
        return f"task_{secrets.token_hex(4)}"


# 运行中聚合事件队列的节奏（秒）。既决定状态行/`TaskList` 的刷新粒度，
# 也决定队列能被及时排空（maxsize=64，生产侧满了会静默丢弃）。
_EVENT_DRAIN_INTERVAL = 0.4


def _coro_state(handle: asyncio.Task) -> str:
    """协程状态字符串（取不到返回空串）。

    `"CORO_CREATED"` = **从未被事件循环调度过** —— 此时 `cancel()` 不会执行任何
    函数体代码，取消只能由调用方自己补收尾（见 `stop`）。
    """
    try:
        return inspect.getcoroutinestate(handle.get_coro())
    except Exception:  # noqa: BLE001 —— 取不到就当作"已启动"，走原有路径
        return ""


async def _drain_events_periodically(queue: asyncio.Queue, bt: BackgroundTask) -> None:
    """任务**运行期间**周期性聚合事件队列。

    没有它时 `_aggregate_events` 只在收尾被调用一次，后果有两个：

    1. 运行中 `bt.tool_count` 恒为 `0`、`bt.last_activity` 恒为 `""` ——
       而 `core/task/tools.py` 把这两个字段**给模型看**（`TaskList`/`TaskGet`），
       模型据此判断"任务在不在动"，拿到的一直是空值；
    2. 队列 `maxsize=64`，生产侧 `except asyncio.QueueFull: pass` **静默丢弃** ——
       没有消费方时超过 64 个事件直接丢掉，连**最终**计数都被截断。

    见 `docs/spec_teammate_status.md` §1.3。
    """
    while True:
        await asyncio.sleep(_EVENT_DRAIN_INTERVAL)
        _aggregate_events(queue, bt)


def _aggregate_events(queue: asyncio.Queue, bt: BackgroundTask) -> None:
    """消费事件队列，聚合 tool_count / last_activity / usage 到 BackgroundTask。"""
    while not queue.empty():
        try:
            item = queue.get_nowait()
            if isinstance(item, tuple):
                kind = item[0]
                if kind == "tool":
                    bt.tool_count += 1
                    bt.last_activity = str(item[1]) if len(item) > 1 else ""
        except asyncio.QueueEmpty:
            break
