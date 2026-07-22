"""OpenAI Chat Completions API 协议客户端。"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator

import httpx

from config.model import ProviderConfig
from conversation.message import APIMessage
from llm import PromptTooLongError
from llm.client import LLMClient
from llm.stream_events import (
    CompletionDone,
    StreamError,
    TextChunk,
    ThinkingChunk,
    ToolUse,
    StreamEvent,
)

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def _normalize_usage(usage: dict | None) -> dict | None:
    """OpenAI 用量字段 → 统一 input_tokens/output_tokens 格式。

    OpenAI 返回 prompt_tokens/completion_tokens，上层（Agent 显示、压缩锚点）
    统一读取 input_tokens/output_tokens。
    """
    if not usage:
        return None
    details = usage.get("prompt_tokens_details", {}) or {}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "cache_read_input_tokens": details.get("cached_tokens", 0),
        "cache_creation_input_tokens": 0,
    }


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions API 协议实现（兼容自定义端点）。"""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        base_url = (config.base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")
        self._base_url = base_url
        self._headers = {
            "Authorization": f"Bearer {config.api_key}",
            "content-type": "application/json",
        }

    async def stream_chat(
        self,
        messages: list[APIMessage],
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        system_blocks: Any = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        api_messages: list[dict] = []

        # 系统提示：支持 PromptAssembly（cached 前缀）和旧版 string
        if system_blocks is not None:
            # cached 块放在前缀位置（OpenAI 自动按前缀缓存）
            for cb in system_blocks.cached:
                api_messages.append({"role": "system", "content": cb.content})
            for ub in system_blocks.uncached:
                api_messages.append({"role": "system", "content": ub.content})
        elif system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})

        api_messages.extend(
            {"role": m.role, "content": m.content} for m in messages
        )

        body: dict = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            body["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers,
                json=body,
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    try:
                        err_data = json.loads(error_body)
                        msg = err_data.get("error", {}).get("message", str(error_body))
                        err_code = err_data.get("error", {}).get("code", "")
                    except (json.JSONDecodeError, AttributeError):
                        msg = error_body.decode(errors="replace")
                        err_code = ""

                    # PTL 错误：抛出哨兵异常供 Agent 紧急压缩
                    if err_code == "context_length_exceeded" or "context_length" in msg.lower():
                        raise PromptTooLongError(msg)

                    yield StreamError(
                        message=msg,
                        code=str(response.status_code),
                    )
                    return

                usage: dict | None = None
                pending_tools: dict[int, dict[str, Any]] = {}
                cached_tokens: int = 0

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    raw = line.removeprefix("data: ")
                    if raw.strip() == "[DONE]":
                        yield CompletionDone(
                            usage=_normalize_usage(usage),
                            cache_read_input_tokens=cached_tokens,
                        )
                        return

                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                            details = usage.get("prompt_tokens_details", {})
                            cached_tokens = details.get("cached_tokens", 0)
                            # 提取缓存命中 token
                            details = usage.get("prompt_tokens_details", {})
                            cached_tokens = details.get("cached_tokens", 0)
                        continue

                    delta = choices[0].get("delta", {})

                    # ── Thinking / reasoning ────────────────────────
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        yield ThinkingChunk(text=reasoning)

                    # ── Text content ──────────────────────────────
                    content = delta.get("content")
                    if content is not None:
                        yield TextChunk(text=content)

                    # ── Tool calls ────────────────────────────────
                    tool_calls = delta.get("tool_calls", [])
                    for tc in tool_calls:
                        idx = tc.get("index", 0)
                        if idx not in pending_tools:
                            pending_tools[idx] = {"id": "", "name": "", "args_parts": []}
                        if tc.get("id"):
                            pending_tools[idx]["id"] = tc["id"]
                        func = tc.get("function", {})
                        if func.get("name"):
                            pending_tools[idx]["name"] = func["name"]
                        if func.get("arguments"):
                            pending_tools[idx]["args_parts"].append(func["arguments"])

                    finish_reason = choices[0].get("finish_reason")
                    if finish_reason is not None:
                        # Emit accumulated tool calls
                        for idx in sorted(pending_tools.keys()):
                            pt = pending_tools[idx]
                            raw_args = "".join(pt["args_parts"])
                            try:
                                parsed = json.loads(raw_args) if raw_args else {}
                            except json.JSONDecodeError:
                                parsed = {}
                            yield ToolUse(id=pt["id"], name=pt["name"], input=parsed)

                        if chunk.get("usage"):
                            usage = chunk["usage"]
                            details = usage.get("prompt_tokens_details", {})
                            cached_tokens = details.get("cached_tokens", 0)
