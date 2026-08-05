"""Skill body 渲染。

负责 $ARGUMENTS 占位符替换和工具白名单提示注入。
"""

from __future__ import annotations

from core.skills.types import SkillDef


def render_body(skill: SkillDef, args: str) -> str:
    """渲染 Skill 正文为最终注入文本。

    1. 替换 $ARGUMENTS 占位符
    2. 若 allowed_tools 非空，在顶部追加工具提示
    3. 若无占位符且 args 非空，在末尾追加 User Request 段

    Args:
        skill: Skill 定义。
        args: 用户传入的参数（可为空）。

    Returns:
        渲染后的完整文本。
    """
    body = skill.prompt_body

    # 工具白名单提示
    if skill.meta.allowed_tools:
        tools_str = ", ".join(skill.meta.allowed_tools)
        tool_hint = (
            f"**This skill is designed to use only these tools: {tools_str}. "
            f"Prefer them over other tools when possible.**\n\n---\n\n"
        )
        body = tool_hint + body

    # $ARGUMENTS 替换
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", args)
    elif args.strip():
        # 无占位符但有参数 → 追加到末尾
        body = f"{body}\n\n## User Request\n\n{args}"

    return body
