"""Skill 系统核心类型定义。

SkillMeta: frontmatter 元信息
SkillDef: 完整 Skill 定义（元信息 + 正文 + 来源）
SkillSource: 来源枚举（项目级 / 用户级）
ActiveEntry: 激活状态条目
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal


class SkillSource(Enum):
    """Skill 来源：项目级优先于用户级。"""

    PROJECT = "project"
    USER = "user"


@dataclass(slots=True)
class SkillMeta:
    """Skill frontmatter 元信息。

    Attributes:
        name: 唯一标识，小写字母数字连字符。
        description: 一句话说明，用于 skill catalog 与补全菜单。
        allowed_tools: 可见工具白名单（空列表 = 不过滤）。
        mode: 执行模式，inline 或 fork，默认 inline。
        fork_context: fork 模式携带历史量（none / recent / full），默认 none。
        model: 可选指定模型名。
    """

    name: str
    description: str
    allowed_tools: list[str] = field(default_factory=list)
    mode: Literal["inline", "fork"] = "inline"
    fork_context: Literal["none", "recent", "full"] = "none"
    model: str | None = None


@dataclass(slots=True)
class SkillDef:
    """完整 Skill 定义 —— 启动时加载到内存。

    Attributes:
        meta: frontmatter 元信息。
        prompt_body: SKILL.md 去 frontmatter 后的 SOP 正文。
        source_path: 源目录绝对路径（用于热重载）。
        source: 来源（项目级 / 用户级）。
        is_directory: 是否为目录型 Skill（含 references 等资源）。
    """

    meta: SkillMeta
    prompt_body: str
    source_path: Path
    source: SkillSource
    is_directory: bool = False


@dataclass(slots=True)
class ActiveEntry:
    """激活 Skill 的运行时快照。

    Attributes:
        name: Skill 名称。
        body: 激活那一刻磁盘上的 SOP 正文。
    """

    name: str
    body: str
