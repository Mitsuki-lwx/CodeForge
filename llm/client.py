"""LLM 客户端抽象基类 + 工厂。

对外接口保持不变（调用方/测试大量依赖）：
  - LLMClient(ABC)，抽象方法 stream_chat(messages, system_prompt, tools, system_blocks)
  - create(config) / create_with_model(config, model)
  - 每个实例暴露 .config

具体协议行为由 AbstractSession 适配器（见 adapters/）在薄子类里实现。
"""

from __future__ import annotations

import re
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from config.model import ProviderConfig
from conversation.message import APIMessage
from llm.stream_events import StreamEvent

# ── 模型名解析（spec_model_resolution）──
#
# 角色 / Skill 声明的 `model` 最终经 `create_with_model` 覆盖 provider 的模型名。
# **子 Agent 与 Skill fork 两条路径都走这一个入口**，所以解析只在这一处做
# —— 避免又变成"靠多处手工设置"，那正是审批机制踩过的坑（新入口必漏）。
#
# `haiku` / `sonnet` / `opus` 是**保留别名**（语义档位，沿袭 Claude Code 生态的
# 角色定义习惯），但它们**不是任何厂商的真实模型名**。项目不内置"别名 → 实际
# 模型"的映射表（映射目标与 provider 强相关），映射由 provider 的
# `model_aliases` 提供。没有映射却把别名发出去 → 必然 `model is not found`，
# 所以这种情况退回主模型并**说明原因**（既不发出去撞错，也不静默改意图）。
RESERVED_MODEL_ALIASES: frozenset[str] = frozenset({"haiku", "sonnet", "opus"})

# 模型名允许的字符。挡住空格 / 换行这类只可能来自写错的输入 —— 发出去只会换来
# 一个难懂的 400/404，不如在本地拦下并说清楚。
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]*$")

# 一次性告警去重：同一个 (provider, model) 只提示一次。
# 否则一个任务里派 N 次子 Agent 就会刷 N 条同样的告警，把真正的错误淹掉。
_warned_once: set[tuple[str, str]] = set()


def _warn_once(provider_name: str, model: str, message: str) -> None:
    key = (provider_name, model)
    if key in _warned_once:
        return
    _warned_once.add(key)
    print(message, file=sys.stderr)


def resolve_model_name(config: ProviderConfig, model: str) -> str:
    """把角色 / Skill 声明的 `model` 解析成**实际发给 API 的模型名**。

    决策顺序（见 `spec_model_resolution.md` 设计骨架表）：

    | 输入 | 结果 |
    |---|---|
    | 空 / `inherit`（不分大小写） | 主模型（`config.model`） |
    | 命中 `config.model_aliases` | 映射值 |
    | 保留别名但未配映射 | 主模型 + 告警（点明要配 `model_aliases`） |
    | 其他合法名称 | **原样**（视作具体模型名） |
    | 含非法字符 | 主模型 + 告警 |

    **纯函数**：不改动 `config`，可重复调用。
    """
    base = config.model
    if not model or not model.strip():
        return base
    candidate = model.strip()

    if candidate.lower() == "inherit":
        return base

    # getattr 而非直接取属性：单测里存在不带该字段的轻量配置对象。
    aliases = getattr(config, "model_aliases", None) or {}
    lowered = candidate.lower()
    if lowered in aliases:
        return aliases[lowered]

    if lowered in RESERVED_MODEL_ALIASES:
        _warn_once(
            config.name,
            candidate,
            f"警告：'{candidate}' 是模型别名，但 provider '{config.name}' 没配 "
            f"model_aliases.{lowered} —— 已退回主模型 '{base}'。"
            f"要让它生效，请在 config.yaml 给该 provider 配 "
            f"model_aliases: {{{lowered}: <实际模型名>}}。",
        )
        return base

    if not _MODEL_NAME_RE.match(candidate):
        _warn_once(
            config.name,
            candidate,
            f"警告：模型名 '{candidate}' 含非法字符 —— 已退回主模型 '{base}'。"
            f"模型名只允许字母/数字/点/下划线/冒号/连字符。",
        )
        return base

    return candidate


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
        """基于现有配置创建客户端，但覆盖模型名（子 Agent / Skill 指定模型用）。

        覆盖前先经 `resolve_model_name` 解析（别名 → 实际模型；无映射的保留别名、
        非法名 → 退回主模型）。**这是唯一的模型覆盖入口**，所以解析放这一处即
        全覆盖，不必在调用点各写一遍。
        """
        from dataclasses import replace

        return cls.create(replace(config, model=resolve_model_name(config, model)))
