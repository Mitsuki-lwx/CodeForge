"""过期会话清理。

删除 session ID 时间戳距当前超过 30 天的会话目录（含 JSONL 与 tool-results）。
仅处理新格式（YYYYMMDD-HHMMSS-xxxx）目录，旧格式目录不删（避免误删遗留数据）。
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认保留天数
CLEANUP_DAYS = 30

_SESSION_ID_RE = re.compile(r"^(\d{8})-(\d{6})-[0-9a-f]{4}$")

# 兼容 on Windows 的时区感知比较：目录名时间是本地时间，无时区信息
_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


def _parse_timestamp(session_id: str) -> datetime | None:
    m = _SESSION_ID_RE.match(session_id)
    if not m:
        return None
    try:
        # session ID 时间戳是本地 naive 时间，与 naive now 直接比较
        return datetime.strptime(  # noqa: DTZ007
            f"{m.group(1)}-{m.group(2)}", _TIMESTAMP_FMT
        )
    except ValueError:
        return None


def cleanup_expired(workspace: str | Path, days: int = CLEANUP_DAYS) -> int:
    """删除超过 days 天的会话目录，返回删除数量。

    单个目录删除失败跳过，不影响其他目录。
    """
    base = Path(workspace).resolve() / ".codeforge" / "sessions"
    if not base.is_dir():
        return 0

    now = datetime.now()  # noqa: DTZ005 —— 与 naive 目录时间戳比较
    removed = 0
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        created = _parse_timestamp(entry.name)
        if created is None:
            continue  # 旧格式 / 无法解析，跳过
        if (now - created).days > days:
            try:
                shutil.rmtree(entry)
            except OSError as e:
                logger.warning("清理会话目录失败 %s: %s", entry, e)
                continue
            removed += 1
            logger.info("已清理过期会话: %s", entry.name)
    return removed
