"""模型切换命令（/model，spec_model_switch）。

方向键重选 provider（复用 select_provider）；取消不切、保持当前模型。
"""

from __future__ import annotations

from core.commands.ui import UI
from tui.provider_select import select_provider


async def handle_model(ui: UI, args: str = "") -> None:
    """/model：方向键重选 provider 并运行时切换（保留当前对话）。"""
    providers = ui.providers()
    if not providers:
        ui.error("未配置 provider")
        return
    try:
        selected = select_provider(providers)
    except SystemExit:
        # select_provider 取消（Esc/Ctrl-C）会 raise SystemExit(0)——视为"取消不切"
        ui.println("已取消，保持当前模型")
        return
    ui.switch_model(selected)
    ui.println(f"已切换: {selected.name} ({selected.model})")
    # 路由启用且切到便宜模型 → 路由停用（cheap is current），提示避免用户蒙在鼓里
    cheap_tier = ui.router_cheap_tier()
    if cheap_tier and getattr(selected, "tier", "") == cheap_tier:
        ui.println(
            "[dim]路由已停用（主模型=便宜）：简单/复杂均走便宜；"
            "切回主模型后路由自动恢复[/]"
        )


__all__ = ["handle_model"]
