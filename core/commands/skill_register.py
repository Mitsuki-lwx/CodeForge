"""Skill → 斜杠命令自动注册。

启动时遍历 Catalog 中的每个 Skill，注册为 /<name> 命令。
支持 reload 时清理旧命令后重建。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from core.commands.types import Command, Kind

if TYPE_CHECKING:
    from core.commands.registry import Registry
    from core.commands.ui import UI
    from core.skills.executor import SkillExecutor
    from core.skills.loader import SkillLoader

logger = logging.getLogger(__name__)

# 跟踪已注册的 Skill 命令名
_REGISTERED_SKILL_NAMES: set[str] = set()


def register_skills_as_commands(
    reg: Registry,
    catalog: SkillLoader,
    executor: SkillExecutor,
) -> None:
    """为 Catalog 中每个 Skill 注册一个 /<name> 命令。

    再次调用时先清理旧命令。

    Args:
        reg: 命令注册中心。
        catalog: Skill 加载器。
        executor: Skill 执行器。
    """
    # 清理旧命令
    remove_skill_commands(reg)

    for skill in catalog.list_all():
        name = skill.meta.name

        # 检查与已有命令冲突
        existing = reg.lookup(name)
        if existing is not None and name not in _REGISTERED_SKILL_NAMES:
            logger.warning(
                "Skill '%s' conflicts with built-in command '%s'. Skipping.",
                name,
                existing.name,
            )
            continue

        mode = skill.meta.mode
        handler = _make_skill_handler(skill.meta.name, mode, executor)

        reg.register(
            Command(
                name=name,
                description=f"{skill.meta.description} [skill]",
                kind=Kind.PROMPT,
                handler=handler,
            )
        )
        _REGISTERED_SKILL_NAMES.add(name)
        logger.debug("Registered skill command: /%s", name)


def remove_skill_commands(reg: Registry) -> None:
    """移除当前已注册的所有 Skill 命令。"""
    for name in list(_REGISTERED_SKILL_NAMES):
        reg.remove(name)
    _REGISTERED_SKILL_NAMES.clear()


def _make_skill_handler(name: str, mode: str, executor: SkillExecutor):
    """创建 Skill 命令的 handler 闭包。

    用默认参数 _name=name 捕获循环变量。
    """

    async def handler(ui: UI, args: str = "", _name: str = name) -> None:
        if mode == "fork":
            # fork 模式：后台子 Agent，结果回流
            asyncio.create_task(_run_fork(ui, _name, args, executor))
        else:
            # inline 模式：渲染 SOP → 激活 → 注入对话触发回合
            await executor.execute_inline(_name, args, ui)

    return handler


async def _run_fork(
    ui: UI,
    name: str,
    args: str,
    executor: SkillExecutor,
) -> None:
    """fork 模式：后台运行子 Agent，结果回流到主对话。"""
    ui.println(f"[skill:{name}] Running in fork mode...")
    try:
        result = await executor.execute_fork(name, args)
        await ui.append_assistant_message(result)
        ui.println(f"[skill:{name}] Completed.")
    except Exception as e:  # noqa: BLE001 — 后台任务兜底，错误上报到 UI
        ui.error(f"[skill:{name}] Failed: {e}")
