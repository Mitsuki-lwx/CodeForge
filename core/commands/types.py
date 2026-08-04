"""命令元数据与类型定义。

Kind 三分类：LOCAL（纯本地）/ UI（影响界面）/ PROMPT（提示词）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.commands.ui import UI

# 命令处理函数：仅依赖 UI 协议，不绑定渲染框架。
# 第二参数是命令参数（无参数命令传 ""）。
Handler = Callable[["UI", str], Awaitable[None]]


class Kind(Enum):
    """命令执行类型。"""

    LOCAL = "local"  # 纯本地：只打印，不改 App、不进 history、不耗 token
    UI = "ui"  # 影响界面：改 App 状态，不进 history
    PROMPT = "prompt"  # 提示词：注入 user 消息 + 触发回合，进 history


@dataclass(slots=True)
class Command:
    """命令元数据。"""

    name: str  # 不带 "/" 前缀，全小写，唯一
    description: str  # 一句话，用于 /help 与补全菜单
    kind: Kind
    handler: Handler
    aliases: list[str] = field(default_factory=list)  # 不带 "/" 前缀，全小写
    arg_hint: str = ""  # 非空表示接受参数（如 /review 的「重点」）
    hidden: bool = False  # /help 与补全不显示，但 dispatcher 仍可命中


@dataclass(slots=True)
class CommandCall:
    """解析器产物：命令名 + 参数。"""

    name: str
    args: str = ""
