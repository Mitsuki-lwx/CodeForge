"""vendor 能力协商（llm.protocol.resolve_adapter_class）单元测试。"""

from __future__ import annotations

from llm.adapters import (
    AnthropicAdapter,
    DeepSeekConversationAdapter,
    OpenAIConversationAdapter,
)
from llm.protocol import resolve_adapter_class


def test_explicit_deepseek_vendor():
    """显式 vendor=deepseek + openai 协议 → DeepSeek 适配器。"""
    subclass = resolve_adapter_class("openai", "deepseek", "some-model", "https://x.example.com")
    assert subclass == DeepSeekConversationAdapter
    assert issubclass(subclass, OpenAIConversationAdapter)


def test_deepseek_vendor_does_not_change_anthropic_wire():
    """vendor=deepseek 对 anthropic 协议无效：wire 仍走 Anthropic（协议是 wire 一级判别）。"""
    assert resolve_adapter_class("anthropic", "deepseek", "deepseek-x", None) == AnthropicAdapter


def test_explicit_anthropic_vendor():
    assert resolve_adapter_class("anthropic", "anthropic", "claude", None) == AnthropicAdapter


def test_explicit_openai_vendor_openai_protocol():
    assert resolve_adapter_class("openai", "openai", "gpt-4", None) == OpenAIConversationAdapter


def test_no_vendor_anthropic():
    assert resolve_adapter_class("anthropic", None, "claude", None) == AnthropicAdapter


def test_no_vendor_openai_plain():
    """无 vendor + 普通 OpenAI 端点 → 基础 OpenAI 适配器。"""
    assert resolve_adapter_class(
        "openai", None, "gpt-4o", "https://api.openai.com/v1"
    ) == OpenAIConversationAdapter


def test_no_vendor_openai_auto_detect_deepseek_by_host():
    """无 vendor + 端点域名含 deepseek → 自动升级 DeepSeek。"""
    assert resolve_adapter_class(
        "openai", None, "deepseek-chat", "https://api.deepseek.com"
    ) == DeepSeekConversationAdapter


def test_no_vendor_openai_auto_detect_deepseek_by_model():
    """无 vendor + 模型名以 deepseek 开头 → 自动升级。"""
    assert resolve_adapter_class(
        "openai", None, "deepseek-v4-flash", "https://custom.example.com"
    ) == DeepSeekConversationAdapter


def test_unknown_vendor_falls_back_to_protocol():
    """未知 vendor 回退到按 protocol 识别。"""
    assert resolve_adapter_class("openai", "notarealvendor", "gpt-4", None) == OpenAIConversationAdapter
    assert resolve_adapter_class("anthropic", "notarealvendor", "claude", None) == AnthropicAdapter
