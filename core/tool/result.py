from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Unified result for all tool executions."""

    success: bool
    data: Any = None
    error: str | None = None
    meta: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.success

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAIL"
        detail = f" data={self.data!r}" if self.data is not None else ""
        detail += f" error={self.error!r}" if self.error else ""
        detail += f" meta={self.meta}" if self.meta else ""
        return f"<ToolResult {status}{detail}>"
