"""记忆索引注入。

把两级 MEMORY.md 索引拼接后注入 long_term_memory 模块；超 25KB 截断。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.notes.store import NoteStore
    from core.prompts.builder import PromptBuilder

# 注入索引大小上限（字节）
INDEX_MAX_BYTES = 25 * 1024

# 截断标注
INDEX_TRUNCATED_MARK = "(index truncated)"


def build_memory_index_text(store: NoteStore) -> str:
    """拼接项目级 + 用户级索引；超限截断并追加标注（F34）。"""
    text = store.full_index()
    if len(text.encode("utf-8")) > INDEX_MAX_BYTES:
        text = (
            text.encode("utf-8")[:INDEX_MAX_BYTES].decode("utf-8", errors="ignore")
            + INDEX_TRUNCATED_MARK
        )
    return text


def load_and_inject_memory(builder: PromptBuilder, store: NoteStore) -> str:
    """加载记忆索引并注入拼装器（F32）。

    Args:
        builder: 系统提示拼装器实例。
        store: 笔记存储。

    Returns:
        注入的索引文本。
    """
    text = build_memory_index_text(store)
    builder.set_injections(memory=text)
    return text
