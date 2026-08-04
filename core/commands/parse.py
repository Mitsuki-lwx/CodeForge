"""斜杠命令输入解析。

parse_line 返回 CommandCall（斜杠命令）或 None（非命令/空输入）。
"""

from __future__ import annotations

from core.commands.types import CommandCall


def parse_line(text: str) -> CommandCall | None:
    """解析斜杠命令输入。

    - 空/纯空白 → None（上层早返回，不参与分发）
    - 不以 `/` 开头 → None（上层作为普通消息送 Agent）
    - `/name [args]` → CommandCall(name 小写, args)
    - `/` 或 `/ ` → CommandCall(name="", args="")（lookup 必然 miss → 未知命令）
    """
    stripped = text.strip()
    if not stripped or not stripped.startswith("/"):
        return None
    rest = stripped[1:]
    if not rest or not rest.strip():
        return CommandCall(name="", args="")
    name, _, args = rest.partition(" ")
    return CommandCall(name=name.lower(), args=args.strip())
