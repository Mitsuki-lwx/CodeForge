"""统一流式事件类型。

LLM 客户端将各协议的 SSE 流式响应解析为统一的内部事件类型，
上层代码（对话管理器、TUI）只认这些类型，不依赖任何 SDK。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TextChunk:
    """正文文本增量片段。"""
    text: str


@dataclass
class ThinkingChunk:
    """模型扩展思考（reasoning）文本增量片段。"""
    text: str


@dataclass
class ToolUse:
    """工具调用信号（在流结束前发出，携带完整 tool_use 信息）。"""
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class CompletionDone:
    """流式回复结束信号。"""
    usage: Optional[dict[str, Any]] = None
    stop_reason: Optional[str] = None
    cache_creation_input_tokens: int = 0
    """缓存写入 token 数（Anthropic: cache_creation_input_tokens）。"""
    cache_read_input_tokens: int = 0
    """缓存命中 token 数（Anthropic: cache_read_input_tokens, OpenAI: cached_tokens）。"""


@dataclass
class StreamError:
    """流式过程中发生的错误。"""
    message: str
    code: Optional[str] = None


StreamEvent = TextChunk | ThinkingChunk | ToolUse | CompletionDone | StreamError
"""统一流式事件联合类型。"""
