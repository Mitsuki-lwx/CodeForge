"""能力协商：protocol + vendor → 匹配的 Adapter 类。

rules：
  - 显式 vendor 优先：deepseek → DeepSeekConversationAdapter；anthropic →
    AnthropicAdapter；openai → OpenAIConversationAdapter（vendor 与 protocol
    不一致时以 vendor 的 wire 为准，仅用于协商 adapter，不改 config）。
  - 未填 vendor 时按 protocol 自动识别；若 protocol=="openai" 且 base_url 域名
    含 deepseek，upgrade 到 DeepSeek 适配器（thinking reasoning 回传）。
  - 兜底：未知 vendor/无法识别 → 按 protocol 选 base adapter。

thinking 开关本身不参与协商（它由 config.thinking 在 adapter.build_request 内
判定），此处只管"上游是哪个厂商"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm.adapters.base import Adapter


def resolve_adapter_class(
    protocol: str,
    vendor: str | None,
    model: str = "",
    base_url: str | None = None,
) -> type[Adapter]:
    """根据协议/厂商/模型/端点返回匹配的 Adapter 类。

    **protocol 是 wire 格式的一级判别**——它决定走 Anthropic 还是 OpenAI 系。
    vendor 只在同一协议族内做细化（如 OpenAI 系里区分 DeepSeek vs 官方）。
    因此 vendor=deepseek 对 anthropic 协议无意义，仍走 AnthropicAdapter。
    """
    from llm.adapters import (
        AnthropicAdapter,
        DeepSeekConversationAdapter,
        OpenAIConversationAdapter,
    )

    # 1) anthropic 协议 → 恒为 Anthropic（vendor 不改 wire）
    if protocol == "anthropic":
        return AnthropicAdapter

    # 2) openai 协议 → 依据 vendor / 端点 / 模型选定 OpenAI 系适配器
    if protocol == "openai":
        v = (vendor or "").lower()
        if v == "deepseek":
            return DeepSeekConversationAdapter
        if v and v != "openai":
            # 未知 vendor：落到自动识别
            pass
        host = (base_url or "").lower()
        if "deepseek" in host or model.lower().startswith("deepseek"):
            return DeepSeekConversationAdapter
        return OpenAIConversationAdapter

    # 3) 兜底
    return OpenAIConversationAdapter
