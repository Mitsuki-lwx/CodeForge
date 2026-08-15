"""Trace 可观测性 / 审计子系统。

把分散在工具执行、权限决策、hook 拦截、上下文压缩、agent 错误/结束处的
结构化事件收敛到单条 per-session 的 audit 流,供排查与审计回放。

三个模块:
- events.py    —— TraceEvent 数据类与序列化(schema)
- writer.py    —— 独立 audit JSONL 写入器(同步落盘、崩溃容忍)
- context.py   ——(后续 T3)单写入器管理与 span 注入

当前阶段(T1+T2)只建数据层:events + writer。
"""

from .events import (
    AgentEndEvent,
    AgentErrorEvent,
    CompactEvent,
    HookEvent,
    PermissionEvent,
    TraceEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from .writer import TraceWriter
from .reader import AuditNotFoundError, children_of, iter_session, session_summary

__all__ = [
    "TraceEvent",
    "ToolStartEvent",
    "ToolEndEvent",
    "PermissionEvent",
    "HookEvent",
    "CompactEvent",
    "AgentErrorEvent",
    "AgentEndEvent",
    "TraceWriter",
    "iter_session",
    "session_summary",
    "children_of",
    "AuditNotFoundError",
]
