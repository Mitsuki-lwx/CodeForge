from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.permissions.upgrade import ApprovalUpgrader


@dataclass
class ProgressNote:
    """一次「谁在做什么」的快照。"""

    agent: str  # 子 Agent 的可读名（空串 = 主 Agent）
    action: str  # 正在做什么：等模型响应 / 工具名 / 收尾
    detail: str = ""  # 可选补充（一般为工具参数摘要）
    at: float = 0.0  # `time.monotonic()`，用于判断新鲜度


class ProgressSink:
    """子 Agent 活动状态的汇点 —— 供界面在「长时间静默」时说明在等谁、等什么。

    **为什么不是事件流**：父 Agent 执行工具时是 `await` 状态，它自己的事件流生成器
    正挂在这个 await 上、**不能 yield**，所以子 Agent 的进度不可能经由父的事件流
    实时冒出来（这是实测确认的结构性事实，不是取舍）。要实时就得再开一条并发通道，
    代价是与主渲染抢同一份 console 输出。这里选更省、也更稳的形态：只维护**一份最新
    状态**，界面**主动来读**——交互界面本来就有看门狗在定时醒。

    刻意只留最后一条：它是给"卡住了吗"用的，不是审计日志。
    线程模型：Agent 都在同一事件循环里跑，属性赋值是原子的，故不加锁；
    若日后有跨线程写入，这里需要补锁。
    """

    def __init__(self) -> None:
        self._latest: ProgressNote | None = None

    def note(self, agent: str, action: str, detail: str = "") -> None:
        """记录一次活动。`agent=""` 表示主 Agent。"""
        self._latest = ProgressNote(
            agent=agent, action=action, detail=detail, at=time.monotonic()
        )

    def snapshot(self) -> ProgressNote | None:
        """读最近一条；没有则返回 `None`（调用方据此退回笼统说法）。"""
        return self._latest

    def clear(self) -> None:
        """清空。子 Agent 跑完时调用，免得父回到等待模型时还显示子 Agent 的旧状态。"""
        self._latest = None


@dataclass
class ExecutionContext:
    """Context passed to every tool execution."""

    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    session_id: str = ""
    # 子 Agent 进度汇（可选）。由 `build_session` 建、经父传给子，
    # 供界面说明"在等谁、等什么"；`None` = 不采集（如测试、无人值守）。
    progress: ProgressSink | None = None
    # 审批升级通道（可选）。由界面建，**只由 `_run_foreground` 下传到前台子 Agent**
    # —— 子 Agent 的 `ask` 由此冒泡到主 TUI（spec 能力清单第 9 条的第三层）。
    # 主 Agent 自己不挂（保持 `yield HITLRequired` 原路径不变），后台子 Agent 与
    # 队友也不挂（父已继续跑，没有可弹窗的时机）—— 见 `spec_subagent.md` 附录 A.5.2。
    approval_upgrader: ApprovalUpgrader | None = None
