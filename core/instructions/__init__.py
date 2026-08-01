"""项目指令子包。

提供三处 CODEFORGE.md 的发现、@include 展开与合并。
"""

from __future__ import annotations

from core.instructions.discovery import (
    InstructionFile,
    discover_instruction_files,
)
from core.instructions.include import (
    MAX_INCLUDE_DEPTH,
    load_instructions,
)
from core.instructions.inject import load_and_inject_instructions

__all__ = [
    "MAX_INCLUDE_DEPTH",
    "InstructionFile",
    "discover_instruction_files",
    "load_and_inject_instructions",
    "load_instructions",
]
