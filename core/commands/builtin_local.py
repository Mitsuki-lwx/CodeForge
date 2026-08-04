"""5 条纯本地命令：/help /status /memory /permission /session。

只通过 UI 协议输出，不修改对话历史、不改界面模式、不消耗 token。
"""

from __future__ import annotations

from core.commands.registry import Registry
from core.commands.types import Handler
from core.commands.ui import UI


def make_help_handler(reg: Registry) -> Handler:
    """/help：按字典序列出可见命令的「名字 + 描述」两列对齐。"""

    async def _handler(ui: UI, args: str = "") -> None:
        cmds = reg.visible()
        if not cmds:
            ui.println("（无可用命令）")
            return
        width = max(len(c.name) for c in cmds)
        lines = [f"/{c.name.ljust(width)}  {c.description}" for c in cmds]
        ui.println("\n".join(lines))

    return _handler


async def handle_status(ui: UI, args: str = "") -> None:
    """/status：输出 6 行 key:value（顺序固定）。"""
    lines = [
        "CodeForge Status",
        "",
        f"Mode:      {ui.mode().value}",
        f"Tokens:    {ui.usage_in()} in / {ui.usage_out()} out",
        f"Tools:     {ui.tool_count()} enabled",
        f"Memories:  {len(ui.memory_files())} files",
        f"Model:     {ui.model_name()}",
        f"Directory: {ui.cwd()}",
    ]
    ui.println("\n".join(lines))


async def handle_memory(ui: UI, args: str = "") -> None:
    """/memory：输出项目层 + 用户层记忆文件名列表。"""
    files = ui.memory_files()
    if not files:
        ui.println("无已加载的记忆文件")
        return
    ui.println("\n".join(files))


async def handle_permission(ui: UI, args: str = "") -> None:
    """/permission：输出当前权限模式名。"""
    ui.println(ui.mode().value)


async def handle_session(ui: UI, args: str = "") -> None:
    """/session：输出当前会话存档路径与 session 标识。"""
    ui.println(f"Session: {ui.session_id()}")
    ui.println(f"Path:    {ui.session_path()}")
