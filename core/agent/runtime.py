"""Agent 运行时状态。

SessionRuntime 是跨轮持有的长生命周期状态容器。
TUI Model 持有同一份 SessionRuntime 跨轮复用，
每轮注入 Agent。compact 是逻辑层，对状态零持有、可重入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.context_compression.state import (
    CompactCircuitBreaker,
    ContentReplacementState,
    RecoveryState,
    SessionContext,
)


@dataclass
class SessionRuntime:
    """跨轮持有的长生命周期状态容器。

    由 TUI Model 在启动阶段构造，每轮通过 Agent(runtime=...) 传入。
    compact 子包不持有此类型的引用。
    """
    replacement: ContentReplacementState = field(
        default_factory=ContentReplacementState
    )
    recovery: RecoveryState = field(default_factory=RecoveryState)
    auto_tracking: CompactCircuitBreaker = field(
        default_factory=CompactCircuitBreaker
    )
    session: SessionContext | None = None
    context_window: int = 200000
    usage_anchor: int = 0       # 主对话路径 stream 真实 usage 之和；摘要请求不更新
    anchor_msg_len: int = 0     # anchor 当时 conv 消息条数
    # 记忆：笔记存储（None 表示未启用自动笔记）与轮次计数
    notes: object | None = None  # core.notes.NoteStore 实例
    turn_count: int = 0         # 已完成的 Agent.run 次数（每 5 轮触发记忆更新）
    # Skill：跨轮激活的 Skill 列表（/clear 时清空）
    active_skills: object = field(
        default_factory=lambda: __import__(
            "core.skills.active", fromlist=["ActiveSkills"]
        ).ActiveSkills()
    )
    # Hook：运行时持有的 HookRunner（/clear 时 reset 清 once + 注入）
    hook_runner: object | None = None
    # SubAgent：后台任务完成通知文本列表（TUI 写入，Agent 下次 run 消费）
    pending_reminders: list[str] = field(default_factory=list)
    # asyncio 单线程，无需显式锁

    def reset_for_new_session(self, ses_ctx: SessionContext) -> None:
        """切换到新会话：原子重置压缩子状态与计数（/clear 用）。

        Args:
            ses_ctx: 新的 SessionContext。
        """
        self.replacement = ContentReplacementState()
        self.recovery = RecoveryState()
        self.auto_tracking = CompactCircuitBreaker()
        self.session = ses_ctx
        self.usage_anchor = 0
        self.anchor_msg_len = 0
        self.turn_count = 0
        self.active_skills.clear()
        if self.hook_runner is not None:
            self.hook_runner.reset()
        # context_window 与 notes 保留
