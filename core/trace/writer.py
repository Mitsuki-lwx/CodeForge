"""审计 JSONL 写入器。

写 `audit/<session_id>.jsonl`,只追加不重写;每行一条 TraceEvent JSON。
对齐 core/archive/writer.py:Writer 的持久性约定:
  - append 模式 "a" + 长期持有句柄
  - 每次追加后 flush + os.fsync(审计优先,崩溃最多丢最后一条未写完)
  - 线程安全:threading.Lock 保护写路径

本类同时持有会话内 `sequence` 计数,保证会话内序号单调递增。
启动时若文件已存在则从尾部续写(sequence 从文件里已写入的最大值+1 起),不清空。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .events import TraceEvent

AUDIT_DIRNAME = "audit"


def _read_max_existing_sequence(path: Path) -> int:
    """读回文件里已写入的最大 sequence,供续写从 max+1 起。坏行跳过。"""
    if not path.exists():
        return 0
    max_seq = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    seq = json.loads(line).get("sequence", 0)
                except (ValueError, json.JSONDecodeError):
                    continue  # 尾行未写完,忽略
                if isinstance(seq, int) and seq > max_seq:
                    max_seq = seq
    except OSError:
        return 0
    return max_seq


class TraceWriter:
    """单会话审计写入器。"""

    def __init__(self, session_id: str, audit_dir: str | Path | None = None) -> None:
        root = Path(audit_dir) if audit_dir else Path.home() / ".codeforge"
        self._dir = root / AUDIT_DIRNAME
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{session_id}.jsonl"
        self._session_id = session_id
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        self._lock = threading.Lock()
        self._seq = _read_max_existing_sequence(self._path)
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def session_id(self) -> str:
        return self._session_id

    def record(self, event: TraceEvent | dict) -> None:
        """同步追加一条事件。分配 session_id + sequence;失败只记日志不抛出。

        返回空——调用方不依赖写结果;trace 失败绝不阻断主流程(见 spec 解耦要求)。
        """
        if self._closed:
            return
        try:
            if not event.session_id:  # type: ignore[union-attr]
                event.session_id = self._session_id  # type: ignore[union-attr]
            if event.sequence == 0:  # type: ignore[union-attr]
                self._seq += 1
                event.sequence = self._seq  # type: ignore[union-attr]
            if event.ts == 0:  # type: ignore[union-attr]
                event.ts = int(time.time() * 1000)  # type: ignore[union-attr]
            data = event.to_dict() if isinstance(event, object) and hasattr(event, "to_dict") else event
            line = json.dumps(data, ensure_ascii=False) + "\n"
            with self._lock:
                self._file.write(line)
                self._file.flush()
                os.fsync(self._file.fileno())
        except Exception:  # noqa: BLE001 —— trace 失败只记日志,绝不抛出
            # 不 import logging,避免循环依赖;静默失败(调用方不依赖)
            pass

    def write(self, data: dict) -> None:
        """追加一条已构造好的 dict 行(兼容 dict 输入、无 session/sequence 注入)。"""
        if self._closed:
            return
        try:
            line = json.dumps(data, ensure_ascii=False) + "\n"
            with self._lock:
                self._file.write(line)
                self._file.flush()
                os.fsync(self._file.fileno())
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        """关闭句柄(幂等)。"""
        self._closed = True
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
