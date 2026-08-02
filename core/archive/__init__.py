"""会话存档子包。

JSONL 追加写入、会话列表扫描、恢复与清理。
"""

from __future__ import annotations

from core.archive.cleanup import CLEANUP_DAYS, cleanup_expired
from core.archive.reader import RECOVERY_STALE_HOURS, RestoreResult, restore_session
from core.archive.session_list import SessionItem, list_sessions
from core.archive.writer import CONVERSATION_FILENAME, Writer, serialize_message

__all__ = [
    "CLEANUP_DAYS",
    "CONVERSATION_FILENAME",
    "RECOVERY_STALE_HOURS",
    "RestoreResult",
    "SessionItem",
    "Writer",
    "cleanup_expired",
    "list_sessions",
    "restore_session",
    "serialize_message",
]
