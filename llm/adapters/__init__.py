"""协议/厂商适配器包。

每个 Adapter 描述一个协议（+可选 vendor）的 wire 封装与响应解析。
"""

from __future__ import annotations

from llm.adapters.anthropic import AnthropicAdapter
from llm.adapters.base import Adapter
from llm.adapters.openai_base import OpenAIConversationAdapter
from llm.adapters.openai_deepseek import DeepSeekConversationAdapter

__all__ = [
    "Adapter",
    "AnthropicAdapter",
    "DeepSeekConversationAdapter",
    "OpenAIConversationAdapter",
]
