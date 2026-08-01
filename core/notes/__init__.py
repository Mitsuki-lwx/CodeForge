"""自动笔记子包。

笔记存储（NoteStore）、索引注入、后台记忆更新器。
"""

from __future__ import annotations

from core.notes.inject import (
    INDEX_MAX_BYTES,
    build_memory_index_text,
    load_and_inject_memory,
)
from core.notes.store import NOTE_TYPES, NoteStore
from core.notes.updater import (
    MEMORY_AUTO_TURNS,
    MEMORY_KEYWORDS,
    should_trigger_memory,
    update_memory,
)

__all__ = [
    "INDEX_MAX_BYTES",
    "MEMORY_AUTO_TURNS",
    "MEMORY_KEYWORDS",
    "NOTE_TYPES",
    "NoteStore",
    "build_memory_index_text",
    "load_and_inject_memory",
    "should_trigger_memory",
    "update_memory",
]
