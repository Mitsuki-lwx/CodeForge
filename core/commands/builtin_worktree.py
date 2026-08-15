"""/worktree 管理命令 —— 创建 / 进入 / 退出 / 删除 / 列出 Worktree。

用户通过此命令手动管理隔离工作目录。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.commands.ui import UI


async def handle_worktree(ui: UI, args: str = "") -> None:
    """分发 /worktree <subcommand>。

    Args:
        ui: UI 协议。
        args: 子命令 + 参数。
    """
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        await _cmd_list(ui)
    elif sub == "create":
        await _cmd_create(ui, sub_args)
    elif sub == "enter":
        await _cmd_enter(ui, sub_args)
    elif sub == "exit":
        await _cmd_exit(ui, sub_args)
    elif sub == "delete":
        await _cmd_delete(ui, sub_args)
    else:
        ui.error(
            f"Unknown subcommand: /worktree {sub}. "
            f"Available: list, create <name>, enter <name>, exit <name>, delete <name>"
        )


async def _cmd_list(ui: UI) -> None:
    """列出全部 worktree。"""
    sessions = ui.worktree_list()
    if not sessions:
        ui.println("No worktrees.")
        return
    ui.println("Worktrees:")
    for s in sessions:
        owner = s.get("owner", "agent")
        label = f"[{owner}]"
        ui.println(f"  {s.get('name', '')}  {label}  {s.get('path', '')}  branch={s.get('branch', '')}")


async def _cmd_create(ui: UI, name: str) -> None:
    """创建（或快速恢复）一个 worktree。"""
    name = name.strip()
    if not name:
        ui.error("Usage: /worktree create <name>")
        return
    result = await ui.worktree_create(name)
    if result.get("ok"):
        ui.println(f"Worktree created: {result.get('name')} at {result.get('path')}")
    else:
        ui.error(f"Failed to create worktree: {result.get('reason', 'unknown error')}")


async def _cmd_enter(ui: UI, name: str) -> None:
    """进入一个 worktree。"""
    name = name.strip()
    if not name:
        ui.error("Usage: /worktree enter <name>")
        return
    msg = await ui.worktree_enter(name)
    if msg:
        ui.error(msg)
    else:
        ui.println(f"Entered worktree: {name}")


async def _cmd_exit(ui: UI, name: str) -> None:
    """退出一个 worktree。"""
    name = name.strip()
    if not name:
        ui.error("Usage: /worktree exit <name>")
        return
    msg = await ui.worktree_exit(name)
    if msg:
        ui.println(msg)
    else:
        ui.println(f"Exited worktree: {name}")


async def _cmd_delete(ui: UI, name: str) -> None:
    """删除一个 worktree（含变更保护）。"""
    name = name.strip()
    if not name:
        ui.error("Usage: /worktree delete <name>")
        return
    msg = await ui.worktree_delete(name)
    if msg:
        ui.error(msg)
    else:
        ui.println(f"Deleted worktree: {name}")
