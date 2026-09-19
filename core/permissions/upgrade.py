"""审批升级通道 —— 让子 Agent 的 `ask` 冒泡到上层（主 TUI）决策。

## 为什么不能走事件流

子 Agent 在 `_run_loop` 里**内部消费**自己 `_execute_tools` 的事件，它 yield 出来的
`HITLRequired` 无人读取；而父 Agent 执行工具时处于 `await` 状态，**不能 yield**。
所以子 Agent 的审批必须另开一条**独立等待通道**，这是结构性的，不是取舍。

## 形态选择（实测，非推演）

探针 `.workbuddy-ai/probe_approval_bridge.py` 复刻「父 await 子」的场景：

| 形态 | 走通 | 副作用 |
| --- | --- | --- |
| 同步阻塞回调 | ✅ | ❌ 期间事件循环心跳 **0 次**（整 loop 冻结，连带冻住后台子 Agent 与看门狗） |
| **异步队列 + 独立消费者 Task** | ✅ | ✅ 心跳 3 次，loop 正常 |
| 队列但消费者靠主协程驱动 | ❌ 死锁 | 主协程正卡在 `await` 子 Agent 上 |

故：消费者必须是**独立 Task**，由界面在启动时创建。

## fail-closed

没有消费者在跑时 `request()` **立即返回拒绝**，不退化成无限等待 ——
"投出去没人接就挂死"正是本通道要修掉的老问题。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from core.permissions.hitl import HITLChoice, HITLRequest, HITLResponse

logger = logging.getLogger(__name__)

# 对齐项目既有的「超时 120 秒自动切后台」约定。
DEFAULT_APPROVAL_TIMEOUT_S = 120.0


@dataclass
class ApprovalOutcome:
    """一次升级审批的结果。

    `reason` 在 `allowed=False` 时**必须**是可读的原因（用户拒绝 / 超时 /
    无消费者）—— 父 Agent 要靠它向用户解释"这步为什么没做成"，裸的
    `"User denied"` 区分不出这三种情况。

    `choice` 透传用户的原始选择，让调用方（子 Agent）能自己落实
    「本会话允许」——**必须记到子 Agent 自己的权限账本上**，不能记到主
    Agent 的：那会让"为子 Agent 放行"变成"给主 Agent 也放行"，是反向越权。
    """

    allowed: bool
    reason: str = ""
    choice: str = ""


class ApprovalUpgrader:
    """把 `ask` 级审批从子 Agent 转给上层消费者（主 TUI）。

    用法::

        upgrader = ApprovalUpgrader()
        task = asyncio.create_task(upgrader.serve(handler))  # 界面侧，独立 Task
        ...
        outcome = await upgrader.request(req)                # 子 Agent 侧
        task.cancel()

    `handler` 是**同步** callable：`(HITLRequest) -> HITLResponse`，
    由消费者放到线程里跑 —— 弹窗要等用户按键，直接在协程里同步调用会把
    整个事件循环冻住（就是上表第一行被否决的那个形态）。
    """

    def __init__(self, timeout: float = DEFAULT_APPROVAL_TIMEOUT_S) -> None:
        self._timeout = timeout
        self._requests: asyncio.Queue = asyncio.Queue()
        self._serving = False
        self._task: asyncio.Task | None = None

    @property
    def serving(self) -> bool:
        """是否有消费者在跑。没跑时 `request()` 一律 fail-closed。"""
        return self._serving

    def start(self, handler: Callable[[HITLRequest], HITLResponse]) -> None:
        """在当前事件循环里起消费者任务（幂等）。

        界面侧调用。**必须在事件循环里调**，否则拿不到 running loop。
        """
        if self._task is None or self._task.done():
            # **先置位再建任务**：`create_task` 只是把协程排进就绪队列，任务真正跑
            # 第一步之前 `serving` 还是 False，而请求完全可能在这中间到达——那样会
            # 被误判成"无人可问"而 fail-closed（实测踩到，用例 A 直接失败）。
            self._serving = True
            self._task = asyncio.create_task(self.serve(handler))

    async def stop(self) -> None:
        """停掉消费者任务并等它退出（幂等，可在关闭路径上安全调用）。"""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 —— 收尾失败不该中断关闭流程
            logger.warning("审批消费者退出异常：%s", e)
        finally:
            self._serving = False

    @property
    def timeout(self) -> float:
        return self._timeout

    async def serve(self, handler: Callable[[HITLRequest], HITLResponse]) -> None:
        """消费者循环：取请求 → 弹窗 → 回填应答。直到被 cancel。"""
        self._serving = True
        try:
            while True:
                req, fut = await self._requests.get()
                if fut.done():
                    # 已超时/被取消 —— 用户不必再看到它。
                    continue
                try:
                    resp = await asyncio.to_thread(handler, req)
                except Exception as e:  # noqa: BLE001 —— 消费者失败不该掀掉回合
                    logger.warning("审批消费者处理失败：%s", e)
                    if not fut.done():
                        fut.set_result(None)
                    continue
                if not fut.done():
                    fut.set_result(resp)
        finally:
            self._serving = False

    async def request(self, req: HITLRequest) -> ApprovalOutcome:
        """子 Agent 侧：投递请求并等应答。所有失败路径都 fail-closed。"""
        if not self._serving:
            return ApprovalOutcome(
                allowed=False,
                reason="审批无人可问（审批通道没有消费者），按拒绝处理",
            )

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._requests.put((req, fut))

        try:
            resp = await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            return ApprovalOutcome(
                allowed=False,
                reason=f"审批超时（{self._timeout:.0f}s 无应答），按拒绝处理",
            )

        if resp is None:
            return ApprovalOutcome(
                allowed=False, reason="审批处理失败，按拒绝处理"
            )
        if resp.choice == HITLChoice.DENY:
            return ApprovalOutcome(
                allowed=False, reason="用户在审批中拒绝了该调用"
            )
        return ApprovalOutcome(allowed=True, choice=resp.choice.value)
