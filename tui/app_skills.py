"""`CodeForgeApp` 的 Skill / Hook 视图命令 —— `/skill`、`/hooks`。

从 `tui/app.py` 抽出：这些方法只读 `skill_loader` / `runtime` / `hook_runner`，
不参与会话编排。

用 Mixin：`self.` 不变、调用点零改动、行为逐字不变。
"""

from __future__ import annotations


class SkillsHooksMixin:
    """Skill 与 Hook 的只读视图。"""

    def list_catalog_skills(self) -> list[dict]:
        """列出 Catalog 中所有 Skill（name/description/source/mode）。"""
        if self.skill_loader is None:
            return []
        result = []
        for s in self.skill_loader.list_all():
            result.append(
                {
                    "name": s.meta.name,
                    "description": s.meta.description,
                    "source": self.skill_loader.get_source_label(s.meta.name),
                    "mode": s.meta.mode,
                }
            )
        return result

    def list_active_skills(self) -> list[str]:
        """列出当前已激活 Skill 的名字列表。"""
        if self.runtime is not None:
            return self.runtime.active_skills.names()
        return []

    def clear_active_skills(self) -> None:
        """清空当前已激活的全部 Skill。"""
        if self.agent is not None:
            self.agent.clear_active_skills()

    async def append_assistant_message(self, text: str) -> None:
        """向主对话追加一条 assistant 消息（fork 模式回流用）。

        add_assistant_message 会通过 on_append 回调自动写入会话存档，
        无需手动 writer.append。
        """
        self.conversation.add_assistant_message(text)

    # ── Hook 系统 ──

    def hook_sources(self) -> list[str]:
        return self.hook_runner.sources if self.hook_runner else []

    def hook_rules(self) -> list:
        return self.hook_runner.rules if self.hook_runner else []
