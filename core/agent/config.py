"""Agent 配置 —— 所有可调参数集中管理。

沿用 ch03 风格，所有阈值为内置常量，不通过配置文件调整。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentConfig:
    """Agent Loop 的行为参数。"""

    max_iterations: int = 25
    """单次用户请求的最大 ReAct 迭代轮次（兜底安全网）。"""

    unknown_tool_threshold: int = 3
    """连续请求不存在的工具达到此值后终止循环。"""

    tool_timeout_seconds: float = 30.0
    """每个工具执行的默认超时（可由工具自身 timeout_seconds 覆盖）。"""

    retry_delay_base: float = 1.0
    """工具执行重试的退避基础（秒）。"""

    result_preview_max_chars: int = 2000
    """工具结果在**事件/落盘**中携带的预览长度上限。

    ★ 为什么是 2000 而不是最初的 120：截断发生在**脱敏之前**
    （`agent.py` 先 `content[:N]` 造 `result_preview`，`TraceWriter` 之后才脱敏），
    所以窗口外的凭据**根本不会落盘，脱敏覆盖不到** ——
    读 audit 的人既看不到明文、也看不到任何"这里曾有过 key"的痕迹。
    探针证据：`.workbuddy-ai/decide_preview_gap.py`（凭据埋在第 1029 字符，
    120 窗口下零痕迹，2000 窗口下被遮成 `[REDACTED:...]`）。

    取 2000 的代价边界：脱敏 0.425ms，仍低于同一次写入的 `os.fsync`（0.55ms）
    ⇒ 不成为新瓶颈。再往上（8000 → 1.77ms）反而超过 fsync。

    ★ 这与 TUI 显示长度是**两个独立常量**（`tui/app.py::_short_preview`，60 字、
    只取首行）—— 放宽本项**不改变任何用户可见输出**。
    改动前请先读 `docs/spec_redact_gaps.md` §1.2。
    """
