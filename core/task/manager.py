"""后台任务管理器。

管理子 Agent 后台任务的全生命周期：创建、执行、停止、续派。
通过 asyncio.Queue 通知任务完成，TUI 消费后注入主对话。
"""

from __future__ import annotations

import asyncio
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
    """任务状态不允许当前操作（如 send_message 给非 COMPLETED 任务）。"""


class TaskNotFoundError(Exception):
    """任务 ID 或 name 未找到。"""


class BackgroundTaskManager:
    """管理后台任务。协程安全（单事件循环）。

    提供 launch / adopt_running / stop / send_message / get / list 等操作，
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

        Args:
            task_id: 任务 ID。

        Returns:
            True 如果找到并发出取消请求。
        """
        bt = self._tasks.get(task_id)
        if bt is None:
            return False
        if bt.handle is not None and not bt.handle.done():
            bt.handle.cancel()
        return True

    async def send_message(self, name: str, message: str) -> str:
        """向已完成的存活后台 Agent 续派新任务。

        Args:
            name: 任务名称（Agent 工具 name 参数）。
            message: 新任务描述。

        Returns:
            task_id（与原来相同）。

        Raises:
            TaskNotFoundError: name 未找到。
            TaskBusyError: 任务状态不是 COMPLETED。
        """
        task_id = self._by_name.get(name)
        if task_id is None:
            raise TaskNotFoundError(f"no task with name '{name}'")

        bt = self._tasks.get(task_id)
        if bt is None:
            raise TaskNotFoundError(f"task '{task_id}' no longer exists")

        if bt.status != TaskStatus.COMPLETED:
            raise TaskBusyError(
                f"task '{name}' is {bt.status.name}, not COMPLETED"
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
