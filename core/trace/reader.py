"""Trace 读取 / 导出接口。

读取端独立于 writer,逐行解析 audit/<session>.jsonl。
纯标准库,不引入外部依赖。

- iter_session(session_id)    :逐行迭代 dict 事件(坏行跳过)
- session_summary(session_id) :聚合计数
- children_of(parent_span_id) :按父 span 过滤出全部子孙事件(T6 预留;父=空串时返回顶层)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Optional

from .writer import AUDIT_DIRNAME


class AuditNotFoundError(FileNotFoundError):
    """指定会话的 audit 文件不存在。"""


def _audit_path(session_id: str, audit_dir: str | Path | None = None) -> Path:
    root = Path(audit_dir) if audit_dir else Path.home() / ".codeforge"
    return root / AUDIT_DIRNAME / f"{session_id}.jsonl"


def iter_session(
    session_id: str, audit_dir: str | Path | None = None
) -> Iterator[dict]:
    """逐行读回会话 audit 事件;坏行(半截 JSON)静默跳过。"""
    path = _audit_path(session_id, audit_dir)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue  # 尾行未写完,忽略


def session_summary(session_id: str, audit_dir: str | Path | None = None) -> dict:
    """聚合一份会话审计摘要。

    Returns:
        {"session_id", "events", "tools", "tool_duration_ms", "tokens",
         "denied", "started_at"}。文件不存在返回空计数摘要(不抛)。
    """
    path = _audit_path(session_id, audit_dir)
    if not path.exists():
        return {
            "session_id": session_id,
            "events": 0,
            "tools": 0,
            "tool_duration_ms": 0,
            "tokens": {},
            "denied": 0,
            "started_at": None,
        }

    events = 0
    tools = 0
    tool_duration = 0
    denied = 0
    tokens: dict = {}
    started_at: int | None = None
    for ev in iter_session(session_id, audit_dir):
        events += 1
        if started_at is None:
            started_at = ev.get("ts")
        evt = ev.get("event")
        if evt == "tool_end":
            tools += 1
            d = ev.get("duration_ms")
            if isinstance(d, int):
                tool_duration += d
        if evt == "permission" and ev.get("decision") == "deny":
            denied += 1
        tok = ev.get("token")
        if isinstance(tok, dict):
            for k, v in tok.items():
                tokens[k] = tokens.get(k, 0) + v
    return {
        "session_id": session_id,
        "events": events,
        "tools": tools,
        "tool_duration_ms": tool_duration,
        "tokens": tokens,
        "denied": denied,
        "started_at": started_at,
    }


def children_of(
    parent_span_id: str, session_id: str, audit_dir: str | Path | None = None
) -> list[dict]:
    """返回所有 parent_span_id 匹配的事件(某父下的全部调用)。

    parent_span_id="" 时返回全部顶层事件(未显式关联的)。
    """
    p = parent_span_id
    return [ev for ev in iter_session(session_id, audit_dir) if ev.get("parent_span_id", "") == p]
