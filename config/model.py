from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderConfig:
    """单个 LLM 服务提供商的配置。"""

    name: str
    protocol: str  # "anthropic" | "openai"
    model: str
    api_key: str
    base_url: Optional[str] = None
    thinking: bool = False
    context_window: int = 0  # 上下文窗口大小（token），0 表示走协议默认
