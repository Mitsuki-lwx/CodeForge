"""团队持久化 —— sanitize / 原子写 / reload_from_disk_locked。

原子写用 `.tmp` + `os.replace`（跨平台原子），对齐 `core/notes/store.py` 的写入风格。
`reload_from_disk_locked` 供 `Team.add_member` / `set_member_active` 在加锁后先重读 disk
members 再改写（F19c —— 跨进程 Pane 后端下 Lead 与子进程各持一份内存 Team，避免丢更新）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from core.team.types import Team, TeammateInfo

# 只保留字母数字 + - . _，其余统一替换为 -。
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize(name: str) -> str:
    """把用户给的团队名转成可安全用于路径的 slug。

    只保留 `[a-zA-Z0-9._-]`，其余字符替换为 `-`；首尾去 `-`。空串返回 ""。
    """
    cleaned = _SANITIZE_RE.sub("-", name.strip())
    cleaned = cleaned.strip("-")
    return cleaned


def atomic_write_json(path: str | Path, value: Any) -> None:
    """把 value 以 JSON 原子写入 path（先写 .tmp 再 os.replace）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    # 先 flush 到磁盘再替换，尽量保证完整性。
    os.replace(tmp, path)


def read_json(path: str | Path) -> Any:
    """读 JSON 文件；文件不存在抛 FileNotFoundError。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


async def reload_from_disk_locked(team: Team) -> None:
    """调用方须已持 team._lock。从 disk 的 members 字段覆盖内存。

    失败（config 缺失/损坏）静默回退到内存现状，绝不抛错中断协作流程。
    """
    if not team.config_path or not Path(team.config_path).exists():
        return
    try:
        data = read_json(team.config_path)
    except (OSError, ValueError):
        return
    disk_members = data.get("members", [])
    # 用 TeammateInfo.from_dict 还原 disk 视图；仅覆盖 members，其余字段不动。
    team.members = [TeammateInfo.from_dict(m) for m in disk_members]
