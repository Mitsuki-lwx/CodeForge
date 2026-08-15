"""事件 payload 构造与序列化。

HookContext 是事件分派时携带的上下文数据；
条件求值（field_value）与动作输入（env / stdin）都取自它。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class HookContext:
    """事件 payload 载体。字段按 spec 事件 payload schema 划分。"""

    event: str
    session_id: str = ""
    cwd: str = ""
    mode: str = ""
    # 工具级
    tool_name: str = ""
    input: dict = field(default_factory=dict)
    tool_result: str = ""
    is_error: bool = False
    # 消息级
    user_input: str = ""
    content: str = ""
    # 系统级
    error_code: str = ""
    trigger: str = ""
    before_tokens: int = 0
    after_tokens: int = 0
    # 计数
    turn_count: int = 0
    iteration: int = 0


def field_value(ctx: HookContext, field: str) -> str:
    """按字段路径取值，供条件匹配。路径不存在按空串处理（F13）。"""
    if field.startswith("input."):
        return str(ctx.input.get(field[len("input.") :], ""))
    if field == "input":
        return json.dumps(ctx.input, sort_keys=True)
    if field == "is_error":
        return "True" if ctx.is_error else "False"
    return str(getattr(ctx, field, ""))


def context_to_env(ctx: HookContext) -> dict[str, str]:
    """命令动作的环境变量（关键字段扁平化，N6 稳定键名）。"""
    return {
        "CODEFAULT_EVENT": ctx.event,
        "CODEFAULT_TOOL": ctx.tool_name,
        "CODEFAULT_INPUT_JSON": json.dumps(ctx.input, sort_keys=True),
        "CODEFAULT_USER_INPUT": ctx.user_input,
        "CODEFAULT_CONTENT": ctx.content,
        "CODEFAULT_ERROR_CODE": ctx.error_code,
        "CODEFAULT_TRIGGER": ctx.trigger,
        "CODEFAULT_SESSION_ID": ctx.session_id,
        "CODEFAULT_CWD": ctx.cwd,
        "CODEFAULT_MODE": ctx.mode,
        "CODEFAULT_TURN_COUNT": str(ctx.turn_count),
        "CODEFAULT_ITERATION": str(ctx.iteration),
    }


def context_to_stdin(ctx: HookContext) -> str:
    """命令动作的标准输入：完整事件 JSON，键按字典序稳定输出（N12）。"""
    return json.dumps(asdict(ctx), sort_keys=True)
