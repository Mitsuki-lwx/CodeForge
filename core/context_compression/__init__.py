"""上下文压缩子包。

提供两层上下文压缩机制：
1. 第一层预防（offload_and_snip）：工具结果超阈值落盘 + 预览替换
2. 第二层兜底（manage_context）：LLM 摘要压缩 + 恢复

对外暴露 manage_context 作为唯一编排入口。
"""

from __future__ import annotations

from core.context_compression.compact import (
    ManageInput,
    ManageOutput,
    TriggerKind,
    manage_context,
)
from core.context_compression.state import (
    CompactCircuitBreaker,
    ContentReplacementState,
    FileReadRecord,
    RecoveryState,
    SessionContext,
    new_session_context,
)

__all__ = [
    "CompactCircuitBreaker",
    "ContentReplacementState",
    "FileReadRecord",
    "ManageInput",
    "ManageOutput",
    "RecoveryState",
    "SessionContext",
    "TriggerKind",
    "manage_context",
    "new_session_context",
]
