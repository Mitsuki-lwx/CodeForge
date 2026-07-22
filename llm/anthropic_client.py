"""Anthropic Messages API 协议客户端。"""

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

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


def _format_tool(tool_def: dict[str, Any]) -> dict[str, Any]:
    """Convert a tool definition to the Anthropic tools API format.

    The Tool interface exposes input_schema() as a JSON Schema dict;
    Anthropic expects it under the key ``input_schema``.
    """
    return {
        "name": tool_def["name"],
        "description": tool_def.get("description", ""),
        "input_schema": tool_def.get("input_schema", {}),
    }


class AnthropicClient(LLMClient):
    """Anthropic Messages API 协议实现。"""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        base_url = (config.base_url or DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")
        self._base_url = base_url
        self._headers = {
            "x-api-key": config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def stream_chat(
        self,
        messages: list[APIMessage],
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        system_blocks: Any = None,
    ) -> AsyncGenerator[StreamEvent, None]:

        # ── 构建请求体 ──
        api_messages: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role}
            if isinstance(m.content, str):
                entry["content"] = m.content
            else:
                entry["content"] = m.content
            api_messages.append(entry)

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
            "max_tokens": 8192,
            "stream": True,
        }

        # 系统提示：支持 PromptAssembly + 旧版 string
        if system_blocks is not None:
            body["system"] = self._build_system_text(system_blocks)
        elif system_prompt:
            body["system"] = system_prompt

        if self.config.thinking:
            body["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        if tools:
            body["tools"] = [_format_tool(t) for t in tools]

        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/messages",
                headers=self._headers,
                json=body,
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    try:
                        err_data = json.loads(error_body)
                        msg = err_data.get("error", {}).get("message", str(error_body))
                    except (json.JSONDecodeError, AttributeError):
                        msg = error_body.decode(errors="replace")

                    # PTL 错误：抛出哨兵异常供 Agent 紧急压缩
                    if "prompt is too long" in msg.lower():
                        raise PromptTooLongError(msg)

                    yield StreamError(
                        message=msg,
                        code=str(response.status_code),
                    )
                    return

                # ── 解析流式事件 ──
                pending_tool: dict[int, dict[str, Any]] = {}
                usage: dict | None = None
                stop_reason: str | None = None
                cache_creation: int = 0
                cache_read: int = 0

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    raw = line.removeprefix("data: ")
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")

                    if event_type == "message_start":
                        msg_obj = event.get("message", {})
                        usage = msg_obj.get("usage", usage)
                        if usage:
                            cache_creation = usage.get("cache_creation_input_tokens", 0)
                            cache_read = usage.get("cache_read_input_tokens", 0)

                    elif event_type == "content_block_start":
                        block = event.get("content_block", {})
                        idx = event.get("index", 0)
                        block_type = block.get("type")

                        if block_type == "tool_use":
                            pending_tool[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input_parts": [],
                            }
                        elif block_type == "text":
                            text = block.get("text", "")
                            if text:
                                yield TextChunk(text=text)

                    elif event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        delta_type = delta.get("type")
                        idx = event.get("index", 0)

                        if delta_type == "text_delta":
                            yield TextChunk(text=delta.get("text", ""))

                        elif delta_type == "thinking_delta":
                            thinking = delta.get("thinking", "")
                            if thinking:
                                yield ThinkingChunk(text=thinking)

                        elif delta_type == "input_json_delta":
                            if idx in pending_tool:
                                pending_tool[idx]["input_parts"].append(
                                    delta.get("partial_json", "")
                                )

                        # thinking_signature_delta — 签名仅用于后续轮次回传，这里不展示

                    elif event_type == "content_block_stop":
                        idx = event.get("index", 0)
                        if idx in pending_tool:
                            pt = pending_tool.pop(idx)
                            raw_input = "".join(pt["input_parts"])
                            try:
                                parsed_input = json.loads(raw_input) if raw_input else {}
                            except json.JSONDecodeError:
                                parsed_input = {}
                            yield ToolUse(
                                id=pt["id"],
                                name=pt["name"],
                                input=parsed_input,
                            )

                    elif event_type == "message_delta":
                        delta = event.get("delta", {})
                        stop_reason = delta.get("stop_reason", stop_reason)
                        msg_usage = event.get("usage")
                        if msg_usage:
                            # message_delta 的 usage 通常只含 output_tokens，
                            # 需与 message_start 的 input_tokens 合并而非替换
                            usage = {**(usage or {}), **msg_usage}
                            cache_creation += msg_usage.get(
                                "cache_creation_input_tokens", 0
                            )
                            cache_read += msg_usage.get("cache_read_input_tokens", 0)

                    elif event_type == "message_stop":
                        yield CompletionDone(
                            usage=usage,
                            stop_reason=stop_reason,
                            cache_creation_input_tokens=cache_creation,
                            cache_read_input_tokens=cache_read,
                        )
                        return

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _build_system_text(system_blocks: Any) -> str:
        """将 PromptAssembly 合并为纯文本 system prompt。

        兼容不支持 system content blocks 数组的端点（如 DeepSeek）。
        """
        parts: list[str] = []
        for cb in system_blocks.cached:
            parts.append(cb.content)
        for ub in system_blocks.uncached:
            parts.append(ub.content)
        return "\n\n".join(parts)
