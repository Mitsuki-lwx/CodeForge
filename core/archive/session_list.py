"""会话列表扫描。

扫描 .codeforge/sessions/ 下的有效会话，提取标题/相对时间/模型/大小，
供 /resume 列表展示。仅识别新格式 session ID（YYYYMMDD-HHMMSS-xxxx）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from core.archive.writer import CONVERSATION_FILENAME

# 新格式 session ID：20260601-143022-a1b2
_SESSION_ID_RE = re.compile(r"^(\d{8})-(\d{6})-[0-9a-f]{4}$")

# 列表标题截断长度
TITLE_MAX_CHARS = 50


@dataclass
class SessionItem:
    """会话列表条目。"""

    session_id: str
    title: str
    relative_time: str
    model: str
    size: int
    mtime: float


def _sessions_dir(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / ".codeforge" / "sessions"


def _relative_time(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} minutes ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hours ago"
    days = hours // 24
    return f"{days} days ago"


def _truncate(text: str, limit: int = TITLE_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _first_user_line(jsonl: Path) -> tuple[str, str]:
    """读取首条 role=user 消息的 content 与 model（供标题/模型标签）。"""
    try:
        with open(jsonl, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "compact":
                    continue
                model = data.get("model") or ""
                if data.get("role") == "user":
                    content = data.get("content", "")
                    if not isinstance(content, str):
                        content = str(content)
                    return _truncate(content), model
                # 首行可能非 user（罕见），但仍可取 model
                if model:
                    return "", model
    except OSError:
        return "", ""
    return "", ""


def list_sessions(workspace: str | Path) -> list[SessionItem]:
    """扫描有效会话，按最后修改时间倒序返回。"""
    base = _sessions_dir(workspace)
    if not base.is_dir():
        return []

    items: list[SessionItem] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        if not _SESSION_ID_RE.match(entry.name):
            continue  # 旧格式会话不展示
        jsonl = entry / CONVERSATION_FILENAME
        if not jsonl.is_file():
            continue
        try:
            size = jsonl.stat().st_size
            mtime = jsonl.stat().st_mtime
        except OSError:
            continue
        title, model = _first_user_line(jsonl)
        items.append(
            SessionItem(
                session_id=entry.name,
                title=title or entry.name,
                relative_time=_relative_time(mtime),
                model=model,
                size=size,
                mtime=mtime,
            )
        )

    items.sort(key=lambda s: s.mtime, reverse=True)
    return items
