"""会话 JSONL 写入器。

对话消息逐条序列化为 JSON 行追加到 <session_dir>/conversation.jsonl。
只追加不重写，崩溃最多丢最后一行不完整写入。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from conversation.message import Message

# JSONL 文件名
CONVERSATION_FILENAME = "conversation.jsonl"


def serialize_message(
    msg: Message,
    *,
    ts: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """把内部 Message 序列化为 JSON 行字段。

    对齐 spec F11：role/content/tool_use_id/tool_name/tool_input/status/id/ts，
    另保留 timestamp/usage 以支持忠实恢复；首条消息携带 model。
    """
    data: dict[str, Any] = {
        "role": msg.role.value,
        "content": msg.content,
        "tool_use_id": msg.tool_use_id,
        "tool_name": msg.tool_name,
        "tool_input": msg.tool_input,
        "status": msg.status.value,
        "id": msg.id,
        "timestamp": msg.timestamp,
        "usage": msg.usage,
        "ts": ts if ts is not None else int(time.time()),
    }
    if model:
        data["model"] = model
    return data


def _write_line(file, lock, data: dict[str, Any]) -> None:
    """加锁追加一行并刷盘。"""
    line = json.dumps(data, ensure_ascii=False)
    with lock:
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())


class Writer:
    """会话 JSONL 写入器。

    线程安全：实际写入用 threading.Lock 保护（写路径来自同步回调，
    F15 允许 asyncio.Lock 或 threading.Lock）。每次追加后 flush + fsync。
    """

    def __init__(self, session_dir: str | Path, model: str = "") -> None:
        self._path = Path(session_dir) / CONVERSATION_FILENAME
        self._model = model
        # 长期持有的文件句柄，进程生命周期内反复追加
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        self._lock = threading.Lock()
        self._wrote_any = self._path.exists() and self._path.stat().st_size > 0

    @property
    def path(self) -> Path:
        return self._path

    def append(self, msg: Message) -> None:
        """追加一条消息；文件为空时首行携带 model。"""
        first = not self._wrote_any
        data = serialize_message(msg, model=self._model if first else None)
        _write_line(self._file, self._lock, data)
        self._wrote_any = True

    def append_compact_marker(self) -> None:
        """压缩整体替换前写标记行（F12）。"""
        _write_line(self._file, self._lock, {"type": "compact", "ts": int(time.time())})

    def close(self) -> None:
        """关闭文件句柄（幂等）。"""
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> Writer:  # noqa: PYI034 —— 返回 self，避免引入 typing.Self 依赖
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
