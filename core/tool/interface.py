from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from jsonschema import ValidationError, validate as jsonschema_validate

from core.tool.context import ExecutionContext
from core.tool.errors import ToolValidationError
from core.tool.result import ToolResult


class Tool(ABC):
    """Abstract base class for all CodeForge tools."""

    # Per-tool timeout in seconds; override in subclasses
    timeout_seconds: float = 30.0

    # Maximum retries for transient errors
    max_retries: int = 2

    # Whether this tool is a system-level tool exempt from skill allowlists
    is_system_tool: bool = False

    @abstractmethod
    def name(self) -> str:
        """Unique name used to reference this tool in the registry."""
        ...

    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this tool does."""
        ...

    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema dict describing the expected input parameters."""
        ...

    @abstractmethod
    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        """Execute the tool with the given context and validated input."""
        ...

    @abstractmethod
    def is_read_only(self) -> bool:
        """Whether this tool only reads data without side effects."""
        ...

    @abstractmethod
    def is_destructive(self) -> bool:
        """Whether this tool can modify or destroy data."""
        ...

    @abstractmethod
    def is_concurrency_safe(self, input: dict) -> bool:
        """Whether multiple calls with the same input can run concurrently.

        Return False for operations that modify a specific resource
        (e.g. writing to a file) to let the framework serialise them.
        """
        ...

    @abstractmethod
    def category(self) -> str:
        """Category this tool belongs to, e.g. 'file', 'code_search', 'shell'."""
        ...

    def validate_input(self, input: dict) -> Optional[str]:
        """Validate *input* against *input_schema*. Return an error string or ``None``."""
        schema = self.input_schema()
        if not schema:
            return None
        try:
            jsonschema_validate(instance=input, schema=schema)
        except ValidationError as e:
            return e.message
        return None
