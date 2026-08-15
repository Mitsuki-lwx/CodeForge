"""Anthropic Messages 协议适配器（含 DeepSeek 的 Anthropic 兼容端点）。

"各家都要自适应" 的 Anthropic 基线：content 块原样透传 + `_format_tool` 的
input_schema；thinking 模式把 `APIMessage.reasoning` 前置为 content[].thinking 块
回传（DeepSeek 的 /anthropic 端点要求）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from conversation.message import APIMessage
from llm.adapters.base import Adapter
from llm.stream_events import (
    CompletionDone,
    StreamEvent,
    TextChunk,
    ThinkingChunk,
    ToolUse,
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


def _build_system_blocks(system_blocks: Any) -> list[dict]:
    """将 PromptAssembly 转成 system content blocks 数组。

    可缓存块（cached）在前，最后一个 cached 块末尾打 cache_control 断点，
    使稳定内容（系统模块/指令/记忆）命中 provider 前缀缓存；不可缓存块
    （环境信息）接在断点之后、不参与缓存。DeepSeek 兼容端点实测支持该断点
    （第 2 次请求 cache_read_input_tokens>0）。
    """
    blocks: list[dict] = []
    for cb in system_blocks.cached:
        blocks.append({"type": "text", "text": cb.content})
    if blocks:
        # 缓存断点打在最后一个可缓存块末尾：断点之后的内容不参与缓存
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    for ub in system_blocks.uncached:
        blocks.append({"type": "text", "text": ub.content})
    return blocks


class AnthropicAdapter(Adapter):
    """Anthropic Messages wire + 解析。"""

    endpoint_path = "/messages"
    ptl_markers: tuple[str, ...] = ("prompt is too long",)

    def build_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def build_base_url(self) -> str:
        return (self.config.base_url or DEFAULT_ANTHROPIC_BASE_URL).rstrip("/")

    async def build_request(
        self,
        messages: list[APIMessage],
        *,
        system_prompt: str,
        system_blocks: Any,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        api_messages: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role}
            content = m.content
            has_tool_use = (
                isinstance(content, list)
                and any(b.get("type") == "tool_use" for b in content)
            )
            # 跳过无意义消息：assistant 且 content 为空（空 str 或空数组 []）、
            # 无 tool_use、无 reasoning —— 否则 to_api_format 的 _as_blocks("") 产生
            # `content: []`，DeepSeek 兼容端点拒绝「all messages must have non-empty content」。
            if (
                m.role == "assistant"
                and not m.reasoning
                and not has_tool_use
                and (content == "" or content == [] or (isinstance(content, list) and not content))
            ):
                continue
            if (
                self.config.thinking
                and m.reasoning
                and m.role == "assistant"
                and not has_tool_use
            ):
                # 纯文本 assistant：thinking 块置于文本之前
                text_blocks = (
                    list(content)
                    if isinstance(content, list) and content
                    else ([{"type": "text", "text": content}] if isinstance(content, str) and content else [])
                )
                entry["content"] = [{"type": "thinking", "thinking": m.reasoning}] + text_blocks
            elif self.config.thinking and m.reasoning and m.role == "assistant":
                # 工具调用消息：顶层 thinking 块 + 原有 text/tool_use 块
                base = list(content) if isinstance(content, list) else (
                    [{"type": "text", "text": content}] if content else []
                )
                entry["content"] = [{"type": "thinking", "thinking": m.reasoning}] + base
            else:
                entry["content"] = content
            api_messages.append(entry)

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": api_messages,
            "max_tokens": 8192,
            "stream": True,
        }

        if system_blocks is not None:
            body["system"] = _build_system_blocks(system_blocks)
        elif system_prompt:
            body["system"] = system_prompt

        if self.config.thinking:
            body["thinking"] = {"type": "enabled", "budget_tokens": 2048}

        if tools:
            body["tools"] = [_format_tool(t) for t in tools]

        return body

    async def emit_events(
        self, lines: AsyncGenerator[str, None]
    ) -> AsyncGenerator[StreamEvent, None]:
        pending_tool: dict[int, dict[str, Any]] = {}
        usage: dict | None = None
        stop_reason: str | None = None
        cache_creation: int = 0
        cache_read: int = 0

        async for line in lines:
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
                    cache_creation += msg_usage.get("cache_creation_input_tokens", 0)
                    cache_read += msg_usage.get("cache_read_input_tokens", 0)

            elif event_type == "message_stop":
                yield CompletionDone(
                    usage=usage,
                    stop_reason=stop_reason,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read,
                )
                return
