"""命令框架。

注册中心、解析、UI 抽象与内置命令。
"""

from __future__ import annotations

from core.commands.builtins import register_builtins
from core.commands.parse import parse_line
from core.commands.registry import CommandRegistryError, Registry
from core.commands.types import Command, CommandCall, Kind
from core.commands.ui import UI, NopUI

__all__ = [
    "UI",
    "Command",
    "CommandCall",
    "CommandRegistryError",
    "Kind",
    "NopUI",
    "Registry",
    "parse_line",
    "register_builtins",
]
