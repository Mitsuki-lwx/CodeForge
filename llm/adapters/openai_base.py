"""OpenAI Chat Completions 协议适配器（所有 OpenAI 兼容端点共同遵守）。

"各家都要自适应" 的基线：content 块数组、tool_use→tool_calls、多 tool_result
展开成多条 role=tool、tool_result→role=tool、usage 规范化 —— 这些是 OpenAI spec
共性，不是 DeepSeek 特有。thinking/`reasoning_content` 回传由子类 openai_deepseek
叠加。

`_to_openai_wire` 与 `_normalize_usage` 保持为模块级函数，供客户端 re-export
兼容既有按名单测。
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


def _drain_pending_tools(pending: dict[int, dict]) -> list[ToolUse]:
    """取出并清空累积中的工具调用（天然幂等：清空后再调返回空表）。

    放在模块级而非内联，是因为它要在两处使用：finish_reason 收尾与 [DONE] 兜底。
    清空动作保证「上游重复给出结束信号」时不会重复发射同一个工具调用。
    """
    out: list[ToolUse] = []
    for idx in sorted(pending.keys()):
        pt = pending[idx]
        raw_args = "".join(pt["args_parts"])
        try:
            parsed = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            parsed = {}
        out.append(ToolUse(id=pt["id"], name=pt["name"], input=parsed))
    pending.clear()
    return out


def _as_text_blocks(text: str) -> list[dict]:
    """非空文本 → OpenAI 内容块数组；空文本 → 空数组。

    OpenAI/DeepSeek 的 ChatCompletionRequestContentBlock 允许 ['text'] 块；
    统一用块数组避免「endpoint 期待 block 却收到裸字符串」的解析歧义。
    """
    return [{"type": "text", "text": text}] if text else []


def _format_tool(tool_def: dict[str, Any]) -> dict[str, Any]:
    """把内部工具定义转成 OpenAI 的 function 格式。

    **为什么必须转**：内部定义沿用 Anthropic 的键名 —— `Tool.input_schema()`
    产出的 dict 里是 `input_schema`（见 `core/agent/agent.py::_build_tool_defs`
    与 `llm/adapters/anthropic.py::_format_tool`，后者要的正是这个键）。
    而 OpenAI 规范要求 `function.parameters`。

    直接透传的后果是**取决于端点宽容度**：商汤的 `deepseek-v4-flash` 会忽略
    这个不认识的键、照常工作（所以一直没暴露）；而同一端点上的 `deepseek-flash`
    会直接回 `inference request is invalid`。实测对照：同一次请求仅把键名换成
    `parameters` 即成功。

    两种键名都接受（`parameters` 优先、回落 `input_schema`），这样调用方无论
    传哪种都能发对；缺 schema 时给一个合法的空对象 schema，不留 `null`。
    """
    schema = tool_def.get("parameters") or tool_def.get("input_schema")
    return {
        "type": "function",
        "function": {
            "name": tool_def.get("name", ""),
            "description": tool_def.get("description", ""),
            "parameters": schema or {"type": "object", "properties": {}},
        },
    }


def _to_openai_wire(m: APIMessage) -> list[dict]:
    """把内部消息转换为 OpenAI wire format。

    内部统一用 Anthropic 内容块（text / tool_use / tool_result）表示消息，
    思考文本单独放在 APIMessage.reasoning。OpenAI 协议不接受 tool_use /
    tool_result 块放在 content 数组里，需转成其原生格式：
      - assistant：text 块留在 content（块数组）；tool_use 块抽到顶层 tool_calls。
      - tool result：每个 block 生成一条 role="tool" 消息，
        tool_call_id 指回对应调用（多个工具调用必须逐条响应）。

    一个内部消息可能展开成多条 wire 消息，故返回 list[dict]。
    """
    base_role = m.role
    reasoning = m.reasoning or ""
    content = m.content

    # 纯文本内容（非工具结构）
    if isinstance(content, str):
        entry: dict = {"role": base_role, "content": _as_text_blocks(content)}
        if base_role == "assistant" and reasoning:
            entry["reasoning_content"] = reasoning
        return [entry]

    if not isinstance(content, list):
        return [{"role": base_role, "content": content or ""}]

    text_blocks: list[dict] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []  # role="tool" 消息

    for block in content:
        btype = block.get("type")
        if btype == "text":
            t = block.get("text", "")
            if t:
                text_blocks.append({"type": "text", "text": t})
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
        elif btype == "tool_result":
            raw = block.get("content", "")
            content_str = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            tool_results.append(
                {"role": "tool", "tool_call_id": block.get("tool_use_id", ""), "content": content_str}
            )

    # 纯工具结果消息（role=user，仅含 tool_result 块）→ 可能展开成多条 tool 消息
    if tool_results and not tool_calls:
        return tool_results

    # assistant（文本 + 工具调用）
    entry: dict = {"role": base_role, "content": text_blocks}
    if tool_calls:
        entry["tool_calls"] = tool_calls
    if base_role == "assistant" and reasoning:
        entry["reasoning_content"] = reasoning
    return [entry]


class OpenAIConversationAdapter(Adapter):
    """OpenAI Chat Completions wire + 解析（不含 thinking 特有行为）。"""

    endpoint_path = "/chat/completions"
    ptl_markers: tuple[str, ...] = ("context_length",)

    def build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "content-type": "application/json",
        }

    def build_base_url(self) -> str:
        return (self.config.base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")

    async def build_request(
        self,
        messages: list[APIMessage],
        *,
        system_prompt: str,
        system_blocks: Any,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
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

        # 每个内部消息可能展开成多条 wire 消息，用 extend 展平
        for api_msg in messages:
            api_messages.extend(_to_openai_wire(api_msg))

        body: dict = {
            "model": self.config.model,
            "messages": api_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if tools:
            body["tools"] = [_format_tool(t) for t in tools]

        return body

    async def emit_events(
        self, lines: AsyncGenerator[str, None]
    ) -> AsyncGenerator[StreamEvent, None]:
        usage: dict | None = None
        cached_tokens: int = 0
        pending_tools: dict[int, dict[str, Any]] = {}

        async for line in lines:
            if not line.startswith("data: "):
                continue

            raw = line.removeprefix("data: ")
            if raw.strip() == "[DONE]":
                # 兜底：个别上游全程不给非空 finish_reason（只在 [DONE] 收尾），
                # 此处把仍累积着的工具调用补发出来，否则会静默丢失。
                for ev in _drain_pending_tools(pending_tools):
                    yield ev
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
                    details = usage.get("prompt_tokens_details", {}) or {}
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
            # 只认「非空」finish_reason 为流结束。
            # 标准 OpenAI 用 null 表示"还在流中"，但部分兼容上游（实测 SenseNova）
            # 把未结束填成空字符串 ""。若按 `is not None` 判定，累积途中每个 chunk
            # 都会触发一次发射——实测一次工具调用被发成 9 个 ToolUse，前几个 input
            # 还是空 {}，上层据此会重复执行工具（写文件场景 = 重复落盘）。
            # _drain_pending_tools 自带清空，多 chunk 重复来到也不会重复发射。
            if finish_reason:
                for ev in _drain_pending_tools(pending_tools):
                    yield ev

                if chunk.get("usage"):
                    usage = chunk["usage"]
                    details = usage.get("prompt_tokens_details", {}) or {}
                    cached_tokens = details.get("cached_tokens", 0)


# 兼容既有按名引用（原 llm.openai_client 导出同名模块内函数）
OPENAI_DEFAULT_BASE_URL = DEFAULT_OPENAI_BASE_URL
