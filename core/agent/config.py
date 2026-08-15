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

    result_preview_max_chars: int = 120
    """工具结果在事件中携带的预览长度上限。"""
