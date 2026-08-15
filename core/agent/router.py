"""模型路由：入口复杂度判断（spec_router）。

用户配置了 `tier=cheap` 的 provider 且与当前主模型不同时启用：
  消息先发往便宜模型判断复杂度——
    SIMPLE → 便宜模型直接回答（不进主对话）
    COMPLEX → 转主对话（用户选的主模型）
未配置 / 单模型 / 判断失败 → 一律转主对话（优雅降级，不丢消息、不阻塞）。
"""

from __future__ import annotations

from typing import Any

from llm.client import LLMClient
from llm.stream_events import CompletionDone, StreamError, TextChunk

# 便宜模型的路由价位标记
CHEAP_TIER = "cheap"

# 复杂度判断 prompt：simple 直接答，complex 返回标记
JUDGE_PROMPT = (
    "You are a lightweight router. Judge whether the user's request is SIMPLE "
    "(can be answered directly, no tools / no multi-step work) or COMPLEX "
    "(needs tools, code changes, or multi-step reasoning).\n\n"
    "If SIMPLE, reply exactly:\n"
    "SIMPLE\n"
    "<your direct answer>\n\n"
    "If COMPLEX, reply exactly:\n"
    "COMPLEX\n\n"
    "User request:\n{message}"
)


def resolve_router(
    providers: list[Any],
    current_provider: Any | None,
    *,
    enabled: bool = False,
    cheap_tier: str = CHEAP_TIER,
) -> tuple[Any, Any] | None:
    """判定是否启用路由。

    默认关：`enabled` 必须为 True 才路由（用户显式配 features.router.enabled）。
    配了 `tier=cheap_tier` 的 provider 且与当前主模型不是同一个 → 返回 (cheap, current)。
    否则返回 None（no-op，全走主模型）。
    """
    if not enabled or current_provider is None:
        return None
    cheap = next((p for p in providers if getattr(p, "tier", "") == cheap_tier), None)
    if cheap is None or cheap is current_provider:
        return None
    return cheap, current_provider


async def judge_and_route(
    cheap: Any,
    message: str,
    *,
    judge_prompt: str | None = None,
    timeout: float = 30.0,
) -> tuple[str, str | None]:
    """用便宜模型判断复杂度。

    judge_prompt 可自定义（用户配 features.router.judge_prompt）；含 `{message}`
    占位符则替换，否则追加用户请求。

    Returns:
        ("simple", answer) 便宜直接答；("complex", None) 转主对话。
    任何异常/超时 → ("complex", None)（转主，不丢消息）。
    """
    try:
        client = LLMClient.create(cheap)
        prompt = judge_prompt or JUDGE_PROMPT
        if "{message}" in prompt:
            prompt = prompt.format(message=message[:4000])
        else:
            prompt = prompt + "\n\nUser request:\n" + message[:4000]
        from conversation.message import APIMessage

        buf: list[str] = []
        async for ev in client.stream_chat(
            [APIMessage(role="user", content=prompt)],
            system_prompt="",
            tools=None,
        ):
            if isinstance(ev, TextChunk):
                buf.append(ev.text)
            elif isinstance(ev, StreamError):
                return ("complex", None)
            elif isinstance(ev, CompletionDone):
                break
        text = "".join(buf).strip()
        if not text:
            return ("complex", None)
        if "COMPLEX" in text.upper():
            return ("complex", None)
        # SIMPLE：去掉标记，剩余作为回答
        answer = text
        if answer.upper().startswith("SIMPLE"):
            answer = answer[len("SIMPLE"):].lstrip(":\n ")
        return ("simple", answer.strip())
    except Exception:  # noqa: BLE001 —— 判断失败转主对话，绝不阻塞
        return ("complex", None)


__all__ = ["CHEAP_TIER", "JUDGE_PROMPT", "judge_and_route", "resolve_router"]
