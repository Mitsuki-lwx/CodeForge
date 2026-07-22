"""LLM 客户端抽象基类。

定义统一的流式对话接口，各协议实现此接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.stream_events import StreamEvent


class LLMClient(ABC):
    """LLM 客户端抽象基类。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[APIMessage],
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        system_blocks: Any = None,  # PromptAssembly | None
    ) -> AsyncGenerator[StreamEvent, None]:
        """向 LLM 发起流式对话请求。

        Args:
            messages: user/assistant 交替的消息列表（支持内容块）。
            system_prompt: 系统提示词，按协议放入对应参数位置。
            tools: 工具定义列表（每个工具含 name, description, input_schema）。
            system_blocks: (可选) PromptAssembly，包含 cached/uncached 块。
                           传此参数时，子类按各自协议处理缓存断点。

        Yields:
            StreamEvent: TextChunk、ToolUse、CompletionDone 或 StreamError。
        """
        ...

    @classmethod
    def create(cls, config: ProviderConfig) -> LLMClient:
        """工厂方法：根据协议类型创建对应客户端实例。"""
        if config.protocol == "anthropic":
            from llm.anthropic_client import AnthropicClient
            return AnthropicClient(config)
        elif config.protocol == "openai":
            from llm.openai_client import OpenAIClient
            return OpenAIClient(config)
        else:
            raise ValueError(f"不支持的协议类型：{config.protocol}")

    @classmethod
    def create_with_model(cls, config: ProviderConfig, model: str) -> LLMClient:
        """基于现有配置创建客户端，但覆盖模型名（Skill 指定模型用）。

        Args:
            config: 基础 ProviderConfig（沿用协议/密钥/端点）。
            model: 覆盖后的模型名。

        Returns:
            使用新模型名的 LLMClient 实例。
        """
        from dataclasses import replace

        return cls.create(replace(config, model=model))
