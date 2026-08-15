"""内置命令一次性注册。"""

from __future__ import annotations

from core.commands.builtin_hooks import handle_hooks
from core.commands.builtin_local import (
    handle_memory,
    handle_observability,
    handle_permission,
    handle_session,
    handle_status,
    make_help_handler,
)
from core.commands.builtin_mcp import handle_mcp
from core.commands.builtin_model import handle_model
from core.commands.builtin_prompt import handle_do
from core.commands.builtin_state import handle_constraint, handle_goal, handle_todo
from core.commands.builtin_team import handle_team
from core.commands.builtin_ui import (
    handle_clear,
    handle_compact,
    handle_exit,
    handle_plan,
    handle_resume,
)
from core.commands.builtin_worktree import handle_worktree
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
            name="observability",
            description="查看可观测性指标/日志/trace 摘要",
            kind=Kind.LOCAL,
            handler=handle_observability,
            aliases=["obs"],
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
    reg.register(
        Command(
            name="hooks",
            description="列出已加载的 hook 列表",
            kind=Kind.LOCAL,
            handler=handle_hooks,
        )
    )
    reg.register(
        Command(
            name="worktree",
            description="管理隔离 worktree（create / enter / exit / delete / list）",
            kind=Kind.LOCAL,
            handler=handle_worktree,
        )
    )
    reg.register(
        Command(
            name="team",
            description="管理团队（list / info <name> / delete <name> [--force] / kill <member>）",
            kind=Kind.LOCAL,
            handler=handle_team,
        )
    )
    reg.register(
        Command(
            name="goal",
            description="查看/设置当前会话目标（/goal [text]）",
            kind=Kind.LOCAL,
            handler=handle_goal,
        )
    )
    reg.register(
        Command(
            name="todo",
            description="查看/添加/勾选会话待办（/todo / /todo add <t> / /todo done <id>）",
            kind=Kind.LOCAL,
            handler=handle_todo,
        )
    )
    reg.register(
        Command(
            name="constraint",
            description="查看/添加/提升硬性约束（/constraint / promote <id> [project|user]）",
            kind=Kind.LOCAL,
            handler=handle_constraint,
        )
    )
    reg.register(
        Command(
            name="model",
            description="运行时切换主模型（方向键重选，保留当前对话）",
            kind=Kind.LOCAL,
            handler=handle_model,
        )
    )
    reg.register(
        Command(
            name="mcp",
            description="列出已配置/已连接的 MCP 服务器及各自工具",
            kind=Kind.LOCAL,
            handler=handle_mcp,
        )
    )
    # /skill 命令在 _run_async 中注册（需要注入 SkillLoader 和 SkillExecutor）
