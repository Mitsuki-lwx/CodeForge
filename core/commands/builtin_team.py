"""/team 管理命令 —— list / info / delete / kill。

用户通过此命令人工介入团队（列出、查看、删除、杀成员）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.commands.ui import UI


async def handle_team(ui: UI, args: str = "") -> None:
    """分发 /team <subcommand>。"""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        await _cmd_list(ui)
    elif sub == "info":
        await _cmd_info(ui, sub_args)
    elif sub == "delete":
        await _cmd_delete(ui, sub_args)
    elif sub == "kill":
        await _cmd_kill(ui, sub_args)
    else:
        ui.error(
            f"Unknown subcommand: /team {sub}. "
            f"Available: list, info <name>, delete <name> [--force], kill <member>"
        )


def _active_count(members: list[dict]) -> int:
    return sum(1 for m in members if m.get("is_active", True) is not False)


async def _cmd_list(ui: UI) -> None:
    """列出全部 Team 摘要。"""
    teams = ui.team_list()
    if not teams:
        ui.println("No teams.")
        return
    ui.println("Teams:")
    for t in teams:
        members = t.get("members", [])
        active = _active_count(members)
        ui.println(
            f"  {t.get('name', '')}  {t.get('backend', '')}  "
            f"{len(members)} 成员  [{active}/{len(members)}] 活跃"
        )


async def _cmd_info(ui: UI, name: str) -> None:
    """展示 Team 详情。"""
    name = name.strip()
    if not name:
        ui.error("Usage: /team info <name>")
        return
    info = ui.team_info(name)
    if info is None:
        ui.error(f"团队不存在: {name}")
        return
    ui.println(f"Team: {info.get('name')} ({info.get('backend')})")
    ui.println(f"config: {info.get('config_path', '')}")
    for m in info.get("members", []):
        active = "active" if m.get("is_active", True) is not False else "idle"
        ui.println(
            f"  {m.get('name', '')}  agent={m.get('agent_id', '')}  "
            f"backend={m.get('backend_type', '')}  {active}  wt={m.get('worktree_path', '')}"
        )


async def _cmd_delete(ui: UI, name: str) -> None:
    """删除 Team（非 force 时拒绝活跃成员）。"""
    name = name.strip()
    if not name:
        ui.error("Usage: /team delete <name> [--force]")
        return
    force = "--force" in name
    clean = name.replace("--force", "").strip()
    msg = await ui.team_delete(clean, force=force)
    if msg:
        ui.error(msg)
    else:
        ui.println(f"团队 {clean} 已删除")


async def _cmd_kill(ui: UI, member: str) -> None:
    """杀一个成员（杀后端 + 从 Team 移除）。"""
    member = member.strip()
    if not member:
        ui.error("Usage: /team kill <member>")
        return
    msg = await ui.team_kill(member)
    if msg:
        ui.error(msg)
    else:
        ui.println(f"成员 {member} 已终止")
