"""MCP 查询命令（/mcp）：列出已配置/已连接的 MCP 服务器及各自工具。"""

from __future__ import annotations

from core.commands.ui import UI


async def handle_mcp(ui: UI, args: str = "") -> None:
    """/mcp：列出 MCP 服务器与各服务器工具。"""
    servers = await ui.mcp_list()
    if not servers:
        ui.println("未配置/未连接 MCP 服务器")
        return
    for s in servers:
        tools = s.get("tools") or []
        ui.println(f"[bold]{s['name']}[/] ({len(tools)} tools)")
        for t in tools[:20]:
            name = t.get("name", "")
            desc = (t.get("description") or "")[:60]
            ui.println(f"  - {name}: {desc}")
        if len(tools) > 20:
            ui.println(f"  … 共 {len(tools)} 个工具（仅列前 20）")


__all__ = ["handle_mcp"]
