from __future__ import annotations


class ToolError(Exception):
    """Base exception for all tool-related errors."""


class ToolNotFoundError(ToolError):
    """Raised when a tool name is not found in the registry."""

    def __init__(self, name: str) -> None:
        self.tool_name = name
        super().__init__(f"Tool not found: {name}")


class ToolValidationError(ToolError):
    """Raised when tool input fails validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class ToolTimeoutError(ToolError):
    """Raised when a tool execution times out."""

    def __init__(self, name: str, timeout: float) -> None:
        self.tool_name = name
        self.timeout = timeout
        super().__init__(f"Tool '{name}' timed out after {timeout}s")


class ToolExecutionError(ToolError):
    """Raised when a tool execution fails with an unexpected error."""

    def __init__(self, name: str, message: str) -> None:
        self.tool_name = name
        super().__init__(f"Tool '{name}' execution failed: {message}")
