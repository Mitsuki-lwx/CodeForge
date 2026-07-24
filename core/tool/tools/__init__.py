"""Built-in tool implementations for CodeForge."""


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


__all__ = ["get_default_registry"]
