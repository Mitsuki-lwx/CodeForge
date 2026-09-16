"""副作用 journal。

它要回答一个问题：**崩溃之前，哪些调用可能已经改动了工作区？**

`conversation.jsonl` 记的是「模型请求了什么」和「工具返回了什么」。但崩溃可以恰好
落在「工具已经改了文件」与「结果已落盘」之间，于是只看对话记录无法区分两种情况：

    A. 工具还没执行，进程就挂了        → 重跑是安全的
    B. 工具已经写完文件，结果没落盘    → 重跑会重复执行副作用

journal 在工具**成功返回之后**立刻追加一条记录（append + flush + fsync）。于是恢复
时「journal 里有记录、对话里没有配对结果」的调用点就是 B 类，必须交给人确认，不能
自动重跑（见 `core/host/recovery.py`）。

只记有副作用的类别（`write` / `command`）。读操作不改动工作区，记进来只会让 journal
随会话长度线性膨胀。

**失败降级**：journal 是辅助证据，写不进去最多让恢复时多问一句「这次调用到底成没
成」；让它把工具执行本身打断是本末倒置。所以 `record()` 不抛异常，只告警并返回
`False`——与 `core/archive/reader.py::_persist_compact` 的降级策略一致。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.permissions.modes import ToolCategory

logger = logging.getLogger(__name__)

# JSONL 文件名（位于 session_dir 下）
JOURNAL_FILENAME = "journal.jsonl"

# 会改动工作区的类别——只有这些值得记
SIDE_EFFECT_CATEGORIES: frozenset[str] = frozenset({"write", "command"})

# 结果摘要截断长度
DEFAULT_SUMMARY_MAX_CHARS = 200


def journal_path(session_dir: str | Path) -> Path:
    """返回 `<session_dir>/journal.jsonl`。"""
    return Path(session_dir) / JOURNAL_FILENAME


@dataclass(frozen=True)
class JournalEntry:
    """journal 里的一条副作用记录。"""

    tool_use_id: str
    tool_name: str
    category: str
    summary: str
    ts: int


def read_journal(session_dir: str | Path) -> list[JournalEntry]:
    """读取 journal；文件不存在返回空列表，坏行跳过。

    坏行必须容忍：崩溃可能正好停在写一半的位置，留下半行 JSON。这与
    `core/archive/reader.py::_read_messages` 的坏行策略一致——半行不能变成异常，
    否则「崩溃后恢复」这件事本身会因为崩溃留下的残迹而失败。
    """
    path = journal_path(session_dir)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    entries: list[JournalEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            entries.append(
                JournalEntry(
                    tool_use_id=str(data["tool_use_id"]),
                    tool_name=str(data.get("tool_name", "")),
                    category=str(data.get("category", "")),
                    summary=str(data.get("summary", "")),
                    ts=int(data.get("ts", 0)),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return entries


class SideEffectJournal:
    """`<session_dir>/journal.jsonl` 追加写入器。

    线程安全：写入用 `threading.Lock` 保护，与 `core/archive/writer.py::Writer` 同构。
    单条记录一次性写入 + `fsync`，崩溃最多丢最后一条（而不是写坏一整块）。
    """

    def __init__(
        self,
        session_dir: str | Path,
        *,
        summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    ) -> None:
        self._path = journal_path(session_dir)
        self._summary_max_chars = summary_max_chars
        self._lock = threading.Lock()
        # 长期持有的文件句柄，进程生命周期内反复追加
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        self._degraded = False

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        category: ToolCategory,
        result: str,
    ) -> bool:
        """追加一条副作用记录。

        Args:
            tool_use_id: 工具调用 id，恢复时靠它与对话里的 `tool_use` 配对。
            tool_name: 工具名（给人看的线索）。
            category: 权限类别，只应传 `SIDE_EFFECT_CATEGORIES` 里的值。
            result: 工具返回内容，截断成摘要后落盘——journal 是线索，不是结果备份，
                完整结果在 `conversation.jsonl` 里。

        Returns:
            是否成功落盘。`False` 表示已降级为告警，调用方不应因此中断执行。
        """
        entry: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "category": category,
            "summary": result[: self._summary_max_chars],
            "ts": int(time.time()),
        }
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                self._file.write(line + "\n")
                self._file.flush()
                os.fsync(self._file.fileno())
        except (OSError, ValueError) as e:
            # 只告警一次：磁盘满之类的问题会持续发生，逐条刷日志会把真正的错误淹掉
            if not self._degraded:
                self._degraded = True
                logger.warning("副作用 journal 写入失败（后续不再重复告警）: %s", e)
            return False
        return True

    def close(self) -> None:
        """关闭文件句柄（幂等）。"""
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> SideEffectJournal:  # noqa: PYI034 —— 返回 self，与 Writer 保持同构
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
