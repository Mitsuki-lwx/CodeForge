"""LLM 客户端抽象基类 + 工厂。

对外接口保持不变（调用方/测试大量依赖）：
  - LLMClient(ABC)，抽象方法 stream_chat(messages, system_prompt, tools, system_blocks)
  - create(config) / create_with_model(config, model)
  - 每个实例暴露 .config

具体协议行为由 AbstractSession 适配器（见 adapters/）在薄子类里实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.stream_events import StreamEvent


class LLMClient(ABC):
    """LLM 客户端抽象基类（协议无关，供消费方与测试稳定依赖）。"""

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
        """向 LLM 发起流式对话请求（契约见各子类/测试）。"""
        ...

    @classmethod
    def create(cls, config: ProviderConfig) -> LLMClient:
        """工厂：按 protocol + vendor 协商出匹配的客户端实现。"""
        from llm.protocol import resolve_adapter_class

        adapter_cls = resolve_adapter_class(
            config.protocol, config.vendor, config.model, config.base_url
        )
        if adapter_cls.__name__.startswith("Anthropic"):
            from llm.anthropic_client import AnthropicClient
            return AnthropicClient(config)
        else:
            from llm.openai_client import OpenAIClient
            return OpenAIClient(config)

    @classmethod
    def create_with_model(cls, config: ProviderConfig, model: str) -> LLMClient:
        """基于现有配置创建客户端，但覆盖模型名（Skill 指定模型用）。"""
        from dataclasses import replace

        return cls.create(replace(config, model=model))
