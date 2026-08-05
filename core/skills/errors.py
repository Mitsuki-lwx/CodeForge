"""Skill 系统自定义异常。

定义在本模块以避免 core.tool.registry ↔ core.skills.executor 循环依赖。
"""

from __future__ import annotations


class SkillDependencyError(Exception):
    """Skill 声明的 allowed_tools 中含不存在的工具。"""


class SkillExecutionError(Exception):
    """Skill 执行过程中发生的错误（fork 模式子 Agent 失败等）。"""
