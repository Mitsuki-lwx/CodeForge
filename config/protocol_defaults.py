"""协议默认值常量。

定义在 config 子包自身，不放入 compact 包，
避免 config → compact 反向依赖。
"""

from __future__ import annotations

# Anthropic 协议默认上下文窗口（token）
DEFAULT_ANTHROPIC_CONTEXT_WINDOW = 200000

# OpenAI 协议默认上下文窗口（token）
DEFAULT_OPENAI_CONTEXT_WINDOW = 128000


def effective_context_window(protocol: str, configured: int = 0) -> int:
    """计算有效的上下文窗口大小。

    优先级：显式配置 > 协议默认 > Anthropic 默认（保守兜底）。

    Args:
        protocol: 协议类型（"anthropic" | "openai"）。
        configured: 用户配置的 context_window 值（0 表示未配置）。

    Returns:
        有效的上下文窗口 token 数。
    """
    if configured > 0:
        return configured
    if protocol == "anthropic":
        return DEFAULT_ANTHROPIC_CONTEXT_WINDOW
    if protocol == "openai":
        return DEFAULT_OPENAI_CONTEXT_WINDOW
    # 未知 protocol：保守使用 Anthropic 默认值
    return DEFAULT_ANTHROPIC_CONTEXT_WINDOW
