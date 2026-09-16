"""run 生命周期状态机。

`RunStore` 只负责持久化，**状态怎么迁移**集中在这里。四条正常路径：

    queued  ──start()──────▶ running
    running ──complete()───▶ completed    进程正常收尾
    running ──fail()───────▶ failed       进程正常退出，但任务本身失败
    running ──interrupt()──▶ interrupted  崩溃 / 被杀 / 信号

外加一条补偿路径 `mark_stale_interrupted()`：扫描「状态还是活跃态、但记录里的 pid
已经不在了」的行，改判 `interrupted`。这条路径非有不可——被 `kill -9` 的进程没有
机会写终态，库里的 `running` 是一句谎话，而恢复逻辑必须先识破它，否则
`codeforge runs` 会永远显示一个根本不存在的 run 在跑。

语义对齐 `Agent._phase`（`core/agent/agent.py`）：那边用 `idle` / `running` 两个
字符串描述「空闲 vs 运行中」，这里把同一件事做成可持久化、可跨进程观察的状态机。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from core.host.proc import pid_alive
from core.host.run_store import (
    ACTIVE_STATUSES,
    RunNotFoundError,
    RunRecord,
    RunStatus,
    RunStore,
)

logger = logging.getLogger(__name__)

# 允许的迁移表。终态是汇点（无出边）——`interrupted` 之后要接着干活，就开一个新
# run（`attach --continue` 的语义），而不是把旧 run 复活：一个 run 对应一次执行，
# 让它原地复活会让「这次到底跑过几遍」变得无法回答。
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    # queued 期间进程就挂了也要能收尾，故允许直接到 interrupted / failed
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.INTERRUPTED, RunStatus.FAILED}
    ),
    RunStatus.RUNNING: frozenset(
        {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}
    ),
    RunStatus.INTERRUPTED: frozenset(),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
}

# 本模块自己的「不改 pid」哨兵。
# 不能借用 RunStore 的私有哨兵透传——它的判等是 `is` 比较，传错对象会被当成
# 一个真实 pid 写进库。
_KEEP_PID: Any = object()


class IllegalTransitionError(RuntimeError):
    """非法的 run 状态迁移（如 `completed` → `running`）。"""

    def __init__(self, run_id: str, frm: RunStatus, to: RunStatus) -> None:
        super().__init__(f"run {run_id}: 不允许从 {frm.value} 迁移到 {to.value}")
        self.run_id = run_id
        self.frm = frm
        self.to = to


class RunLifecycle:
    """run 状态迁移入口。

    `pid` 默认取当前进程：run 记录由将要执行它的进程创建和推进，当前 pid 就是对的
    那个答案；显式传入只在测试（模拟别的进程）或跨进程代管时才需要。
    """

    def __init__(self, store: RunStore, *, pid: int | None = None) -> None:
        self._store = store
        self._pid = os.getpid() if pid is None else pid

    @property
    def store(self) -> RunStore:
        return self._store

    # ── 正常迁移 ────────────────────────────────────────────────

    def start(self, run_id: str) -> RunRecord:
        """`queued` → `running`，并把 pid 刷成当前进程。"""
        return self._transition(run_id, RunStatus.RUNNING, pid=self._pid)

    def complete(self, run_id: str, *, reason: str | None = None) -> RunRecord:
        """正常收尾 → `completed`。"""
        return self._transition(run_id, RunStatus.COMPLETED, reason=reason)

    def fail(self, run_id: str, *, reason: str) -> RunRecord:
        """进程正常退出但任务失败 → `failed`。"""
        return self._transition(run_id, RunStatus.FAILED, reason=reason)

    def interrupt(self, run_id: str, *, reason: str) -> RunRecord:
        """崩溃 / 被杀 / 收到信号 → `interrupted`。"""
        return self._transition(run_id, RunStatus.INTERRUPTED, reason=reason)

    # ── 补偿迁移 ────────────────────────────────────────────────

    def mark_stale_interrupted(self) -> list[RunRecord]:
        """把「活跃态但 pid 已不在」的 run 改判为 `interrupted`。

        扫描**活跃态**（`queued` / `running`）而不只是 `running`：首期没有队列调度
        器，`queued` 只是 `create()` 到 `start()` 之间的短暂窗口，进程在这个窗口里
        挂掉同样会留下一条永远不会推进的谎话。

        只改判、**不重跑**。是否重跑必须由人确认，因为 journal 里可能已经有副作用
        落盘了（见 `core/host/recovery.py`）。

        已知局限：pid 会被操作系统复用。若旧进程已死而其 pid 恰好被新进程占用，这条
        记录会被判为存活、留在 `running`。代价是列表里多一条僵尸记录（不影响写权限，
        写权限由单写者锁保证），因此首期接受该误差，不为此引入进程启动时间比对。

        Returns:
            被改判的记录列表（按扫描顺序）。
        """
        stale: list[RunRecord] = []
        for status in ACTIVE_STATUSES:
            for rec in self._store.list(status=status, limit=None):
                if pid_alive(rec.pid):
                    continue
                try:
                    stale.append(
                        self._transition(
                            rec.id,
                            RunStatus.INTERRUPTED,
                            reason=f"进程 {rec.pid} 已不存在（崩溃或被杀）",
                        )
                    )
                except IllegalTransitionError:
                    # 扫描与改判之间另一进程已把它收尾——不覆盖真实终态
                    logger.debug("run %s 已被其他进程收尾，跳过 stale 改判", rec.id)
        return stale

    # ── 内部 ────────────────────────────────────────────────────

    def _transition(
        self,
        run_id: str,
        to: RunStatus,
        *,
        reason: str | None = None,
        pid: int | None = _KEEP_PID,
    ) -> RunRecord:
        """校验迁移合法性后落库。

        先读一次当前状态再写，是为了把非法迁移挡在库里——`RunStore` 刻意不做这层
        校验，规则只在这里一处。
        """
        current = self._store.get(run_id)
        if current is None:
            raise RunNotFoundError(f"run 不存在: {run_id}")
        if to not in ALLOWED_TRANSITIONS[current.status]:
            raise IllegalTransitionError(run_id, current.status, to)
        if pid is _KEEP_PID:
            return self._store.update_status(run_id, to, exit_reason=reason)
        return self._store.update_status(run_id, to, exit_reason=reason, pid=pid)
