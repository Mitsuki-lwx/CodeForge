"""LoadSkill 工具 —— 按需激活 Skill。

系统级工具（is_system_tool=True），不受 Skill 白名单约束。
模型可调用此工具激活 Skill，将完整 SOP 钉入环境上下文。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult

if TYPE_CHECKING:
    from core.agent.agent import Agent
    from core.skills.loader import SkillLoader


class LoadSkillTool(Tool):
    """按需激活 Skill 的系统工具。

    输入 Skill 名称，从磁盘重读最新 SOP，
    调用 Agent.activate_skill 将 SOP 钉入环境上下文。
    """

    is_system_tool: bool = True

    def __init__(self) -> None:
        super().__init__()
        self._loader: SkillLoader | None = None
        self._agent: Agent | None = None

    def set_loader(self, loader: SkillLoader) -> None:
        """注入 SkillLoader 引用。"""
        self._loader = loader

    def set_agent(self, agent: Agent) -> None:
        """注入 Agent 引用。"""
        self._agent = agent

    # ── Tool interface ──────────────────────────────────────────────

    def name(self) -> str:
        return "LoadSkill"

    def description(self) -> str:
        return (
            "Activate a Skill by name. When the user's request matches an available "
            "Skill, call this tool to load the Skill's full SOP (standard operating "
            "procedure) into the environment context. The Skill's instructions will "
            "then guide subsequent actions. Use LoadSkill for: commit (generate commit "
            "messages), review (code review), test (run and fix tests), or any custom "
            "skill listed in the Available Skills catalog."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the Skill to activate.",
                },
            },
            "required": ["name"],
        }

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def category(self) -> str:
        return "skill"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        """执行 LoadSkill。

        Args:
            context: 执行上下文。
            input: 含 name 键的字典。

        Returns:
            ToolResult — 成功时含短确认信息，失败时含错误描述。
        """
        if self._loader is None or self._agent is None:
            return ToolResult(
                success=False,
                error="LoadSkill not properly initialized (loader or agent not set)",
                meta={"tool": "LoadSkill"},
            )

        name = input.get("name", "").strip()
        if not name:
            return ToolResult(
                success=False,
                error="Skill name is required",
                meta={"tool": "LoadSkill"},
            )

        skill = self._loader.get(name)
        if skill is None:
            available = self._loader.names()
            hint = ""
            if available:
                hint = f" Available skills: {', '.join(available)}."
            return ToolResult(
                success=False,
                error=f"Unknown skill: '{name}'. Use /skill list to see available skills.{hint}",
                meta={"tool": "LoadSkill", "skill": name},
            )

        self._agent.activate_skill(skill.meta.name, skill.prompt_body)

        return ToolResult(
            success=True,
            data=f"Skill '{skill.meta.name}' activated. SOP pinned to environment context.",
            meta={"tool": "LoadSkill", "skill": skill.meta.name},
        )
