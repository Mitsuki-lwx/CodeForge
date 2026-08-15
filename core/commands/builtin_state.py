"""会话状态斜杠命令：/goal /todo /constraint（spec_session_state）。

通过 UI.session_state() 拿 SessionStateStore（未注入时提示未启用）。
"""

from __future__ import annotations

from core.commands.ui import UI


def _store(ui: UI):
    s = ui.session_state()
    if s is None:
        ui.error("会话状态未启用（当前运行未注入 SessionStateStore）")
        return None
    return s


async def handle_goal(ui: UI, args: str = "") -> None:
    """/goal [text]：查看当前目标；带参数则设置（覆盖）。"""
    store = _store(ui)
    if store is None:
        return
    text = (args or "").strip()
    if text:
        store.set_goal(text)
        ui.println(f"目标已设置: {text}")
    else:
        g = store.get_goal()
        ui.println(f"当前目标: {g or '(未设置)'}")


async def handle_todo(ui: UI, args: str = "") -> None:
    """/todo [add <text> | done <id>]：列出待办；add 添加；done 勾选完成。"""
    store = _store(ui)
    if store is None:
        return
    arg = (args or "").strip()
    if not arg:
        todos = store.list_todos()
        if not todos:
            ui.println("待办为空")
        for t in todos:
            mark = "x" if t["done"] else " "
            ui.println(f"[{mark}] {t['text']}")
        return
    if arg.startswith("add "):
        store.add_todo(arg[4:].strip())
        ui.println("已添加待办")
    elif arg.startswith("done "):
        store.toggle_todo(arg[5:].strip(), True)
        ui.println("已勾选完成")
    else:
        store.add_todo(arg)
        ui.println("已添加待办")


async def handle_constraint(ui: UI, args: str = "") -> None:
    """/constraint [text | promote <id> [project|user]]：查看/添加/提升约束。"""
    store = _store(ui)
    if store is None:
        return
    arg = (args or "").strip()
    if not arg:
        for c in store.list_constraints(include_persisted=True):
            ui.println(f"[{c['level']}] {c['text']}")
        return
    if arg.startswith("promote "):
        parts = arg.split()
        if len(parts) < 2:
            ui.error("用法: /constraint promote <id> [project|user]")
            return
        target = parts[2] if len(parts) > 2 else "project"
        if target not in ("project", "user"):
            ui.error(f"提升目标必须为 project 或 user，收到 {target!r}")
            return
        try:
            store.promote_constraint(parts[1].strip(), target)
        except FileNotFoundError as e:
            ui.error(str(e))
            return
        ui.println(f"约束已提升到 {target}（跨会话/跨项目持久）")
        return
    store.add_constraint(arg)
    ui.println("已添加硬性约束（会话级；可用 /constraint promote 提升持久）")


__all__ = ["handle_constraint", "handle_goal", "handle_todo"]
