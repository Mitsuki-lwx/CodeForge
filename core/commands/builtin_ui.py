"""5 条影响界面命令：/exit /plan /compact /resume /clear。

idle 守护由 dispatch_slash 按 Kind 统一处理，handler 不再单独检查。
"""

from __future__ import annotations

from core.commands.ui import UI
from core.permissions.modes import PermissionMode


async def handle_exit(ui: UI, args: str = "") -> None:
    """/exit：关闭进程。"""
    ui.quit()


async def handle_plan(ui: UI, args: str = "") -> None:
    """/plan：切换到计划模式。"""
    ui.set_mode(PermissionMode.PLAN)
    ui.println("已切换到 PLAN 模式")


async def handle_compact(ui: UI, args: str = "") -> None:
    """/compact：手动触发上下文压缩。"""
    await ui.force_compact()


async def handle_resume(ui: UI, args: str = "") -> None:
    """/resume：打开历史会话列表。"""
    await ui.open_resume_menu()


async def handle_clear(ui: UI, args: str = "") -> None:
    """/clear：结束当前会话并开启新会话，同时清空已激活 Skill。"""
    ui.clear_and_new_session()
    ui.clear_active_skills()
    ui.println("已清空当前会话，开启新 session")
