"""Built-in tool implementations for CodeForge.

SKIP_DIRS: 遍历目录时报跳过的重型/嵌入目录,避免把 .venv 等几千文件的
目录递归拉爆工具执行(对齐参考项目 mewcode tools/base.py)。
"""
from __future__ import annotations

SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
})


def _path_climbs_to_skip(path: "Path") -> bool:
    """判断某个 Path 是否位于任一 SKIP_DIRS 之下(按路径段命中即跳过)。"""
    return any(part in SKIP_DIRS for part in path.parts)


def get_default_registry() -> "ToolRegistry":
    """Create and return the global default registry with all built-in tools."""
    from core.tool.registry import ToolRegistry
    from core.tool.tools.bash import BashTool
    from core.tool.tools.edit_file import EditFileTool
    from core.tool.tools.exit_plan_mode import ExitPlanModeTool
    from core.tool.tools.glob_tool import GlobTool
    from core.tool.tools.grep_tool import GrepTool
    from core.tool.tools.read_file import ReadFileTool
    from core.tool.tools.write_file import WriteFileTool

    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(BashTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ExitPlanModeTool())  # 占位，callbacks 由 TUI 后续注入
    return registry


__all__ = ["get_default_registry", "SKIP_DIRS", "_path_climbs_to_skip"]
