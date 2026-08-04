"""1 条提示词命令：/do。

向对话注入固定提示词并触发 Agent 回合；消息与真实用户消息同等持久化。
（/review 已由 Skill 系统的 review Skill 提供，不再硬编码。）
"""

from __future__ import annotations

from core.commands.ui import UI
from core.permissions.modes import PermissionMode

# /do 注入的执行指令（计划内容已在对话上下文中）
EXECUTE_DIRECTIVE = "请按上面的计划开始执行。如需最新信息请先读取相关文件。"


async def handle_do(ui: UI, args: str = "") -> None:
    """/do：退回执行模式并按计划动手。"""
    ui.set_mode(PermissionMode.DEFAULT)
    await ui.inject_and_send("/do", EXECUTE_DIRECTIVE)
