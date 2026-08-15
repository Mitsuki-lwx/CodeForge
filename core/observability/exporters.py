"""JSONL 本地导出器(sinks)。

三个可写目标:spans / logs / metrics,都落在 `~/.codeforge/obs/{subdir}/`。
对齐 core/trace/writer.py 的持久性约定:
  - 只追加、同步 fsync(崩溃最多丢最后一条不完整行)
  - 每行一个合法 JSON、`ensure_ascii=False`
  - 线程安全(threading.Lock)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class JsonlSink:
    """一个 JSONL 目标的写入器。"""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "data.jsonl"
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        self._lock = threading.Lock()
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write_line(self, data: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            line = json.dumps(data, ensure_ascii=False) + "\n"
            with self._lock:
                self._file.write(line)
                self._file.flush()
                os.fsync(self._file.fileno())
        except Exception as e:  # noqa: BLE001 —— 可观测性失败绝不上抛,但记 stderr 便于排查
            import sys

            try:
                print(f"[observability] JSONL write failed: {e}", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._closed = True
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> JsonlSink:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
