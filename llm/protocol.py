"""能力协商：protocol + vendor → 匹配的 Adapter 类。

**规则不再写在本文件里** —— 它们声明在各适配器实现模块的
``@register_adapter`` 装饰器上（见 `llm/adapters/registry.py`）：

  - ``anthropic`` 协议 → `AnthropicAdapter`（恒命中；vendor 不改 wire，
    因此 vendor=deepseek 对 anthropic 协议无意义）
  - ``openai`` 协议 → 先看 vendor 是否 deepseek；未命中再看 base_url / model
    是否像 deepseek（thinking reasoning 回传用）；都不像则用 OpenAI 官方适配器
  - 兜底：未知 protocol/vendor → openai 的默认实现（**并打一条 stderr 告警**）

本模块只保留对外函数名并转发，以免破坏既有调用方与测试。

thinking 开关本身不参与协商（它由 ``config.thinking`` 在
``adapter.build_request`` 内判定），此处只管"上游是哪个厂商"。
"""

from __future__ import annotations

from llm.adapters.registry import (
    AdapterRegistration,
    iter_registrations,
    register_adapter,
    resolve_adapter_class,
    resolve_client,
)

__all__ = [
    "AdapterRegistration",
    "iter_registrations",
    "register_adapter",
    "resolve_adapter_class",
    "resolve_client",
]
