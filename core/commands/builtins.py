"""内置命令一次性注册。"""

from __future__ import annotations

from core.commands.builtin_local import (
    handle_memory,
    handle_permission,
    handle_session,
    handle_status,
    make_help_handler,
)
from core.commands.builtin_prompt import handle_do
from core.commands.builtin_ui import (
    handle_clear,
    handle_compact,
    handle_exit,
    handle_plan,
    handle_resume,
)
from core.commands.registry import Registry
from core.commands.types import Command, Kind


def register_builtins(reg: Registry) -> None:
    """注册 11 条内置命令 + 1 条 skill 管理命令。

    /review 不再是硬编码命令——它现在作为内置 Skill 提供，
    通过 register_skills_as_commands 自动注册。
    """
    reg.register(
        Command(
            name="help",
            description="列出可用命令",
            kind=Kind.LOCAL,
            handler=make_help_handler(reg),
            aliases=["h"],
        )
    )
    reg.register(
        Command(
            name="status",
            description="查看模式/token/工具/记忆/模型/目录",
            kind=Kind.LOCAL,
            handler=handle_status,
            aliases=["stats"],
        )
    )
    reg.register(
        Command(
            name="memory",
            description="列出已加载的记忆文件",
            kind=Kind.LOCAL,
            handler=handle_memory,
            aliases=["notes"],
        )
    )
    reg.register(
        Command(
            name="permission",
            description="查看当前权限模式",
            kind=Kind.LOCAL,
            handler=handle_permission,
            aliases=["mode"],
        )
    )
    reg.register(
        Command(
            name="session",
            description="查看当前会话信息",
            kind=Kind.LOCAL,
            handler=handle_session,
            aliases=["sessions"],
        )
    )
    reg.register(
        Command(
            name="exit",
            description="退出 CodeForge",
            kind=Kind.UI,
            handler=handle_exit,
            aliases=["quit"],
        )
    )
    reg.register(
        Command(
            name="plan",
            description="进入计划模式",
            kind=Kind.UI,
            handler=handle_plan,
        )
    )
    reg.register(
        Command(
            name="compact",
            description="手动触发上下文压缩",
            kind=Kind.UI,
            handler=handle_compact,
            aliases=["compress"],
        )
    )
    reg.register(
        Command(
            name="resume",
            description="恢复历史会话",
            kind=Kind.UI,
            handler=handle_resume,
        )
    )
    reg.register(
        Command(
            name="clear",
            description="结束当前会话并开启新会话",
            kind=Kind.UI,
            handler=handle_clear,
        )
    )
    reg.register(
        Command(
            name="do",
            description="退回执行模式并按计划执行",
            kind=Kind.PROMPT,
            handler=handle_do,
        )
    )
    # /skill 命令在 _run_async 中注册（需要注入 SkillLoader 和 SkillExecutor）
