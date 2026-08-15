"""Agent 异步事件流 —— Agent ↔ UI 之间的解耦契约。

Agent 在 run() 中产出这些事件，UI 层只消费事件、不感知循环内部细节。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ── 流式输出 ──


@dataclass
class TextDelta:
    """正文增量（实时显示）。"""

    text: str


@dataclass
class ThinkingDelta:
    """模型扩展思考增量（单独一块、与正文区分显示）。"""

    text: str


# ── 工具调用 ──


@dataclass
class ToolCallStarted:
    """工具调用开始信号。"""

    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolCallFinished:
    """工具调用结束信号。"""

    tool_use_id: str
    name: str
    input: dict[str, Any]  # 调用输入（渲染工具标题用）
    success: bool
    result_preview: str  # 结果摘要（截断到前 120 字符）
    duration_ms: int = 0


# ── 进度与用量 ──


@dataclass
class IterationUpdate:
    """迭代轮次与累计 token 用量更新。"""

    iteration: int
    total_usage: dict[str, int] = field(default_factory=dict)
    # total_usage: {"input_tokens": ..., "output_tokens": ...}


# ── 压缩状态事件 ──


class CompactPhase(Enum):
    """压缩生命周期阶段。"""
    BEFORE_AUTO = "before_auto"
    AFTER_AUTO = "after_auto"
    BEFORE_EMERGENCY = "before_emergency"
    AFTER_EMERGENCY = "after_emergency"


@dataclass
class CompactEvent:
    """上下文压缩生命周期事件。

    Agent 主循环在自动/紧急压缩前后 emit 此事件，
    供 TUI 渲染"正在压缩..."等状态提示。
    """
    phase: CompactPhase
    before: int = 0
    after: int = 0
    err: Exception | None = None


# ── 结束信号 ──


@dataclass
class AgentFinished:
    """Agent 循环正常结束，携带最终文本回复。"""

    text: str
    total_usage: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    elapsed_s: float = 0.0
    turn_end_reason: str = "completed"  # completed / max-tokens（触顶 sticky）/ blocked


@dataclass
class AgentError:
    """Agent 因异常停止。

    code 取值:
      "max_iterations"  — 达到迭代上限
      "unknown_tool"    — 连续请求未知工具
      "stream_error"    — provider 流出错
      "cancelled"       — 用户取消
      "ptl_error"       — 上下文过长且紧急压缩不可恢复
    """

    message: str
    code: str  # "max_iterations" | "unknown_tool" | "stream_error" | "cancelled" | "ptl_error"


@dataclass
class PlanReady:
    """Plan Mode 下模型调用了 ExitPlanMode，等待用户审批。"""

    plan_path: str
    plan_content: str


@dataclass
class HITLRequired:
    """权限检查返回 ask，需要用户确认。"""

    tool_name: str
    tool_use_id: str
    description: str
    arguments: dict
    risk_hint: str = ""


@dataclass
class HITLResolved:
    """用户已完成 HITL 确认。"""

    tool_use_id: str
    allowed: bool
    choice: str = ""  # "allow_once" | "allow_session" | "allow_save" | "deny"


# ── 联合类型 ──


AgentEvent = (
    TextDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolCallFinished
    | IterationUpdate
    | CompactEvent
    | AgentFinished
    | AgentError
    | PlanReady
    | HITLRequired
    | HITLResolved
)
"""Agent 对外吐出的所有事件联合类型。"""
