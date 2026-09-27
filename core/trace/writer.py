"""审计 JSONL 写入器。

写 `audit/<session_id>.jsonl`,只追加不重写;每行一条 TraceEvent JSON。
对齐 core/archive/writer.py:Writer 的持久性约定:
  - append 模式 "a" + 长期持有句柄
  - 每次追加后 flush + os.fsync(审计优先,崩溃最多丢最后一条未写完)
  - 线程安全:threading.Lock 保护写路径

本类同时持有会话内 `sequence` 计数,保证会话内序号单调递增。
启动时若文件已存在则从尾部续写(sequence 从文件里已写入的最大值+1 起),不清空。

**落盘脱敏**（`docs/spec_show_redact.md` §4.5）:
  - 脱敏在 `json.dumps` **之前**、**在写 try 之外**做 —— 见 `_safe_redact` 的说明
  - 脱敏失败**不写明文**:降级成一条只带标记的行
  - 开关 `CODEFORGE_REDACT=0` / yaml `observability.redact_secrets`;默认开
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from core.observability.redact import redact_config_lazy, redact_mapping

from .events import TraceEvent

logger = logging.getLogger(__name__)

AUDIT_DIRNAME = "audit"

#: 脱敏自身失败时写进该字段,让"这行没脱敏成功"可被事后发现
REDACT_ERROR_KEY = "redact_error"

#: 脱敏失败降级时**允许保留**的字段（都是非内容类：标识与计量）
_SAFE_KEYS = frozenset(
    {"event", "session_id", "sequence", "ts", "duration_ms", "success", "blocked"}
)


def _safe_redact(data: dict[str, Any]) -> dict[str, Any]:
    """对一条即将落盘的事件做脱敏；**脱敏自身失败时绝不返回明文**。

    ★ 为什么必须放在写 try 之外：`record` / `write` 的写路径是
    `except Exception: pass`（审计优先，绝不阻断主流程）。若脱敏放在里面，
    它一旦抛异常就会被**静默吞掉，然后明文照常落盘** —— 而没人知道。

    这里的契约：**要么给脱敏后的数据，要么给一个显式的失败标记**，
    绝不"悄悄放行原文"。

    返回值带 `redact_error` 时，调用方应把它写进该行以便事后发现。
    """
    if not redact_config_lazy():
        return data
    try:
        return redact_mapping(data)
    except Exception as e:  # noqa: BLE001 —— 脱敏失败也绝不放明文
        # 只保留**非内容字段**（事件名/序号/时间/工具名），丢掉所有
        # 可能含敏感内容的长字符串（result_preview / reason / …）。
        safe = {
            k: v
            for k, v in data.items()
            if k in _SAFE_KEYS and isinstance(v, (int, float, bool, type(None)))
        }
        safe["event"] = str(data.get("event") or "unknown")
        safe[REDACT_ERROR_KEY] = f"redaction failed, content dropped: {type(e).__name__}"
        return safe


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
        #: 关闭后仍被提交的事件数。静默丢弃会让"注进去的东西没生效"无从查证
        #: （本轮真链路探针因此连续假红 6 轮），故可计数、可断言。
        self.dropped_after_close = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def session_id(self) -> str:
        return self._session_id

    def _note_dropped(self, kind: str) -> None:
        """记录一次"关闭后写入"。刻意只 debug 不抛异常。

        `agent.run()` 的 finally 已经 `_trace_close()`，收尾期晚到的写入是**正常情况**,
        抛异常会把整个会话炸掉；但静默丢弃会让"注进去的东西没生效"查无实据
        （本轮真链路探针为此连续假红 6 轮），故降级为可查的 debug + 计数。
        """
        self.dropped_after_close += 1
        logger.debug(
            "trace writer 已关闭，丢弃一条 %s（累计 %d 条）", kind, self.dropped_after_close
        )

    def record(self, event: TraceEvent | dict) -> None:
        """同步追加一条事件。分配 session_id + sequence;失败只记日志不抛出。

        返回空——调用方不依赖写结果;trace 失败绝不阻断主流程(见 spec 解耦要求)。
        """
        if self._closed:
            self._note_dropped("record")
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
            # 脱敏在 json.dumps 之前（落盘边界），且**在写 try 的语义之外**
            # —— 见 `_safe_redact` 的 docstring 为什么不能放进 try
            safe = _safe_redact(data)
            line = json.dumps(safe, ensure_ascii=False) + "\n"
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
            self._note_dropped("write")
            return
        try:
            line = json.dumps(_safe_redact(data), ensure_ascii=False) + "\n"
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
