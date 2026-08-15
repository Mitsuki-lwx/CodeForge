"""/hooks 命令：列出已加载的 hook 规则与加载来源。"""

from __future__ import annotations

from core.commands.ui import UI
from core.hooks.rules import HookRule


def _format_rule(rule: HookRule) -> str:
    flags = []
    if rule.once:
        flags.append("once")
    if rule.async_run:
        flags.append("async")
    suffix = f" [{', '.join(flags)}]" if flags else ""
    return f"  {rule.name}  {rule.event}  {rule.action.type}{suffix}"


async def handle_hooks(ui: UI, args: str = "") -> None:
    """/hooks：按事件分组列出已加载规则，末尾标注加载来源文件。"""
    rules = ui.hook_rules()
    if not rules:
        ui.println("No hooks loaded.")
        return

    groups: dict[str, list[HookRule]] = {}
    for rule in rules:
        groups.setdefault(rule.event, []).append(rule)
    for event_rules in groups.values():
        for rule in event_rules:
            ui.println(_format_rule(rule))

    sources = ui.hook_sources()
    if sources:
        ui.println(f"Loaded from: {', '.join(sources)}")
