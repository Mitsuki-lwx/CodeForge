"""指令注入入口。

从三处 CODEFORGE.md 加载并展开指令文本，注入系统提示拼装器。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.instructions.include import load_instructions

if TYPE_CHECKING:
    from core.prompts.builder import PromptBuilder


def load_and_inject_instructions(builder: PromptBuilder, workspace: str) -> str:
    """加载指令文本并注入拼装器（F7）。

    Args:
        builder: 系统提示拼装器实例。
        workspace: 项目根目录。

    Returns:
        加载到的指令文本（供测试/展示；无文件时为空字符串）。
    """
    text = load_instructions(workspace)
    builder.set_injections(instructions=text)
    return text
