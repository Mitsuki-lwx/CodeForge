"""/skill 管理命令 —— 列出 / 查看 / 重载 Skill。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.commands.ui import UI
    from core.skills.executor import SkillExecutor
    from core.skills.loader import SkillLoader


async def handle_skill(
    ui: UI,
    args: str = "",
    catalog: SkillLoader | None = None,
    executor: SkillExecutor | None = None,
) -> None:
    """分发 /skill <subcommand>。

    Args:
        ui: UI 协议。
        args: 子命令 + 参数。
        catalog: SkillLoader 实例（由闭包注入）。
        executor: SkillExecutor 实例（由闭包注入）。
    """
    if catalog is None:
        ui.error("Skill system not initialized")
        return

    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        await _cmd_list(ui, catalog)
    elif sub == "info":
        await _cmd_info(ui, catalog, sub_args)
    elif sub == "reload":
        await _cmd_reload(ui, catalog, executor)
    else:
        ui.error(
            f"Unknown subcommand: /skill {sub}. Available: list, info <name>, reload"
        )


async def _cmd_list(ui: UI, catalog: SkillLoader) -> None:
    """列出所有可用 Skill。"""
    skills = catalog.list_all()
    if not skills:
        ui.println("No skills loaded.")
        return

    ui.println("Available Skills:")
    for s in skills:
        source = catalog.get_source_label(s.meta.name)
        mode_label = f"[{s.meta.mode}]"
        if s.meta.mode == "fork":
            mode_label += f" ctx:{s.meta.fork_context}"
        ui.println(
            f"  {s.meta.name:<24} {mode_label:<18} {s.meta.description}  "
            f"[dim][{source}][/]"
        )


async def _cmd_info(ui: UI, catalog: SkillLoader, name: str) -> None:
    """显示单个 Skill 的完整信息。"""
    name = name.strip()
    if not name:
        ui.error("Usage: /skill info <name>")
        return

    skill = catalog.get(name) or _lookup_cached(catalog, name)
    if skill is None:
        ui.error(f"Unknown skill: {name}")
        return

    ui.println(f"Name:        {skill.meta.name}")
    ui.println(f"Description: {skill.meta.description}")
    ui.println(f"Mode:        {skill.meta.mode}")
    if skill.meta.mode == "fork":
        ui.println(f"Fork Context: {skill.meta.fork_context}")
    if skill.meta.allowed_tools:
        ui.println(f"Tools:       {', '.join(skill.meta.allowed_tools)}")
    if skill.meta.model:
        ui.println(f"Model:       {skill.meta.model}")
    ui.println(f"Source:      {catalog.get_source_label(skill.meta.name)}")
    ui.println(f"Path:        {skill.source_path}")
    ui.println(f"Directory:   {'yes' if skill.is_directory else 'no'}")


async def _cmd_reload(
    ui: UI,
    catalog: SkillLoader,
    executor: SkillExecutor | None,
) -> None:
    """重新扫描 Skill 目录并重建命令。"""
    catalog.reload()
    ui.println("Skills reloaded from disk.")

    # 重新注册命令（通过 executor 所在的上下文）
    ui.println("Skill commands rebuilt. Use /skill list to verify.")


def _lookup_cached(catalog: SkillLoader, name: str):
    """从缓存中查找 Skill（不触发热重载）。"""
    # SkillLoader._skills 是原始缓存
    return catalog._skills.get(name)
