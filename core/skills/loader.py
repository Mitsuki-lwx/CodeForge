"""Skill 加载器 —— 两级路径扫描与热重载。

启动时扫描项目目录和用户目录下的所有 SKILL.md，
按优先级覆盖（项目级 > 用户级），运行时支持热重载。
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.skills.parser import SkillParseError, parse_skill_file
from core.skills.types import SkillDef, SkillSource

logger = logging.getLogger(__name__)

PROJECT_SKILLS_DIR = ".codeforge/skills"
USER_SKILLS_DIR = "~/.codeforge/skills"


class SkillLoader:
    """两级路径 Skill 加载器。

    - 项目级：<work_dir>/.codeforge/skills/
    - 用户级：~/.codeforge/skills/
    - 同名 Skill 项目级覆盖用户级
    - get(name) 每次重读源文件实现热重载，失败回退 _cache
    """

    def __init__(self, work_dir: str | Path) -> None:
        self._work_dir = Path(work_dir).resolve()
        self._project_dir = self._work_dir / PROJECT_SKILLS_DIR
        self._user_dir = Path(USER_SKILLS_DIR).expanduser().resolve()
        self._skills: dict[str, SkillDef] = {}  # 主存储（启动加载）
        self._cache: dict[str, SkillDef] = {}  # 热重载回退缓存

    # ── Public API ──────────────────────────────────────────────────

    def load_all(self) -> None:
        """扫描两级目录，加载全部 Skill。

        先扫项目目录再扫用户目录，后扫到的同名 name 覆盖前者
        （项目级优先）。解析失败的单个文件跳过并记 warning。
        """
        self._skills.clear()
        self._cache.clear()

        # 先扫项目目录
        project_skills = self._scan_directory(self._project_dir, SkillSource.PROJECT)
        # 再扫用户目录
        user_skills = self._scan_directory(self._user_dir, SkillSource.USER)

        # 合并：先放用户级，再放项目级（项目级会覆盖同名）
        for skill in user_skills:
            self._skills[skill.meta.name] = skill
        for skill in project_skills:
            if skill.meta.name in self._skills:
                logger.debug(
                    "Skill '%s': project-level overrides user-level",
                    skill.meta.name,
                )
            self._skills[skill.meta.name] = skill

        # 初始化缓存
        self._cache = dict(self._skills)

    def reload(self) -> None:
        """重新扫描两级目录（用于 /skill reload 和 InstallSkill 后）。"""
        self.load_all()

    def get(self, name: str) -> SkillDef | None:
        """按名字获取 Skill，每次重读源文件实现热重载。

        读取成功时更新 _cache；读取失败时回退 _cache 中的旧版本。

        Args:
            name: Skill 名称（大小写不敏感，实际要求精确匹配）。

        Returns:
            SkillDef 或 None（name 不存在）。
        """
        existing = self._skills.get(name)
        if existing is None:
            return None

        source_path = existing.source_path
        skill_file = source_path / "SKILL.md"

        try:
            fresh = parse_skill_file(skill_file, existing.source)
            self._cache[name] = fresh
            return fresh
        except (SkillParseError, OSError) as e:
            logger.warning(
                "Skill '%s': hot-reload failed (%s), falling back to cached version",
                name,
                e,
            )
            # 回退缓存
            return self._cache.get(name, existing)

    def list_all(self) -> list[SkillDef]:
        """返回按名字字典序排序的全部 Skill 列表。"""
        return sorted(self._skills.values(), key=lambda s: s.meta.name)

    def names(self) -> list[str]:
        """返回按字典序排序的全部 Skill 名字列表。"""
        return sorted(self._skills.keys())

    def get_source_label(self, name: str) -> str:
        """返回 Skill 的来源标签（"project" 或 "user"）。

        Args:
            name: Skill 名称。

        Returns:
            "project" 或 "user"。name 不存在时返回 "unknown"。
        """
        skill = self._skills.get(name)
        if skill is None:
            return "unknown"
        return skill.source.value

    def validate_tools(self, registry) -> list[str]:
        """校验所有 Skill 的 allowed_tools 是否在 registry 中存在。

        对每个不存在的工具名打 warning，并把对应的 Skill 从 catalog 中移除。

        Args:
            registry: ToolRegistry 实例。

        Returns:
            被移除的 Skill 名字列表。
        """
        from core.tool.errors import ToolNotFoundError

        removed: list[str] = []
        for name in list(self._skills.keys()):
            skill = self._skills[name]
            bad_tools: list[str] = []
            for tool_name in skill.meta.allowed_tools:
                try:
                    registry.get(tool_name)
                except (ToolNotFoundError, KeyError):
                    bad_tools.append(tool_name)

            if bad_tools:
                logger.warning(
                    "Skill '%s': allowed_tools references non-existent tools: %s. "
                    "Removing skill from catalog.",
                    name,
                    bad_tools,
                )
                del self._skills[name]
                self._cache.pop(name, None)
                removed.append(name)

        return removed

    # ── Internal ────────────────────────────────────────────────────

    def _scan_directory(self, root: Path, source: SkillSource) -> list[SkillDef]:
        """扫描目录下所有子目录中的 SKILL.md。

        目录不存在时静默跳过。

        Args:
            root: 扫描根目录。
            source: 来源标签。

        Returns:
            成功解析的 SkillDef 列表。
        """
        if not root.is_dir():
            return []

        skills: list[SkillDef] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue

            try:
                skill = parse_skill_file(skill_file, source)
                skills.append(skill)
                logger.debug("Loaded skill '%s' from %s", skill.meta.name, entry)
            except SkillParseError as e:
                logger.warning("Skipping skill '%s': %s", entry.name, e)
            except OSError as e:
                logger.warning("Skipping skill '%s': I/O error: %s", entry.name, e)

        return skills
