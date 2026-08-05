"""InstallSkill 工具 —— 从 URL 远程安装 Skill。

通过 GitHub URL 下载 Skill 目录并安装到 ~/.codeforge/skills/。
安装后自动触发热重载。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult

if TYPE_CHECKING:
    from core.skills.loader import SkillLoader

logger = logging.getLogger(__name__)


class InstallSkillTool(Tool):
    """从 GitHub URL 安装 Skill 的工具。

    支持 skills.sh、github.com tree、raw.githubusercontent.com 三种 URL 格式。
    安装到 ~/.codeforge/skills/<name>/ 后触发 catalog 热重载。
    """

    def __init__(
        self,
        catalog: SkillLoader | None = None,
        work_dir: str | Path = "",
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._work_dir = Path(work_dir) if work_dir else Path.cwd()
        self._on_installed = None  # callable: (skill_name) -> None

    def set_on_installed(self, callback) -> None:
        """设置安装后回调（用于重新注册斜杠命令）。"""
        self._on_installed = callback

    # ── Tool interface ──────────────────────────────────────────────

    def name(self) -> str:
        return "InstallSkill"

    def description(self) -> str:
        return (
            "Install a Skill from a GitHub URL. Supports:\n"
            "- skills.sh/<owner>/<repo>\n"
            "- github.com/<owner>/<repo>/tree/<ref>/<path>\n"
            "- raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>/SKILL.md\n"
            "The skill will be installed to ~/.codeforge/skills/<name>/ and "
            "be immediately available for use."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL of the Skill to install (GitHub or skills.sh).",
                },
            },
            "required": ["url"],
        }

    def is_read_only(self) -> bool:
        return False  # 写盘 + 网络

    def is_destructive(self) -> bool:
        return False  # 只写 ~/.codeforge/skills/

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def category(self) -> str:
        return "skill"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        """执行远程安装。

        Args:
            context: 执行上下文。
            input: 含 url 键的字典。

        Returns:
            ToolResult — 成功时含安装确认信息。
        """
        from core.skills.install import install_from_url

        url = input.get("url", "").strip()
        if not url:
            return ToolResult(
                success=False,
                error="URL is required",
                meta={"tool": "InstallSkill"},
            )

        install_root = Path("~/.codeforge/skills").expanduser().resolve()

        try:
            skill_name = await install_from_url(url, install_root)
        except ValueError as e:
            return ToolResult(
                success=False,
                error=f"Invalid URL: {e}",
                meta={"tool": "InstallSkill", "url": url},
            )
        except Exception as e:
            logger.exception("InstallSkill failed for URL: %s", url)
            return ToolResult(
                success=False,
                error=f"Install failed: {e}",
                meta={"tool": "InstallSkill", "url": url},
            )

        # 触发热重载
        if self._catalog is not None:
            try:
                self._catalog.reload()
            except Exception as e:  # noqa: BLE001 — reload 失败不阻断安装结果
                logger.warning("Catalog reload after install failed: %s", e)

        # 回调 → 重新注册斜杠命令
        if self._on_installed is not None:
            try:
                self._on_installed(skill_name)
            except Exception as e:  # noqa: BLE001 — 回调失败不阻断安装结果
                logger.warning("Install callback failed: %s", e)

        return ToolResult(
            success=True,
            data=(
                f"Skill '{skill_name}' installed successfully to "
                f"{install_root / skill_name}.\n"
                f"Use /skill reload to refresh, or /{skill_name} to invoke."
            ),
            meta={"tool": "InstallSkill", "skill": skill_name, "url": url},
        )
