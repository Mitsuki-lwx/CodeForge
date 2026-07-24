from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExecutionContext:
    """Context passed to every tool execution."""

    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    session_id: str = ""
