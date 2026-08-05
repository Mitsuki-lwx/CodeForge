"""Skill 系统核心包。

提供 Skill 的定义、解析、加载、激活管理和执行能力。
"""

from __future__ import annotations

from core.skills.active import ActiveSkills
from core.skills.errors import SkillDependencyError, SkillExecutionError
from core.skills.executor import SkillExecutor, filter_tool_registry
from core.skills.loader import SkillLoader
from core.skills.parser import SkillParseError, parse_skill_file, substitute_arguments
from core.skills.types import ActiveEntry, SkillDef, SkillMeta, SkillSource

__all__ = [
    "ActiveEntry",
    "ActiveSkills",
    "SkillDef",
    "SkillDependencyError",
    "SkillExecutionError",
    "SkillExecutor",
    "SkillLoader",
    "SkillMeta",
    "SkillParseError",
    "SkillSource",
    "filter_tool_registry",
    "parse_skill_file",
    "substitute_arguments",
]
