"""摘要 Prompt 模板。

维护摘要 prompt 的全文文案、对话序列化、模型输出解析。
纯模板 + 字符串解析，不依赖外部状态。
"""

from __future__ import annotations

import re
from typing import Any

from conversation.message import Message

# ── 摘要指令模板 ─────────────────────────────────────────────────

SUMMARY_INSTRUCTION: str = """你必须不调用任何工具。你的任务是对以下对话生成结构化摘要。

第一步：输出 <analysis>...</analysis>，在这里写分析草稿。分析对话内容，确保覆盖所有 9 个分区。草稿会被丢弃，不会存入最终摘要。

第二步：基于分析草稿，输出 <summary>...</summary>，正式摘要约 5000 汉字 / 20000 字符。

<summary> 必须严格按照以下 9 个固定小节顺序输出，每节以 "## N 标题" 开头：

## 1 主要请求和意图
用户到底想做什么

## 2 关键技术概念
讨论过的重要技术点

## 3 文件和代码段
涉及哪些文件，关键代码片段要保留

## 4 错误和修复
遇到了什么错，怎么解决的

## 5 问题解决过程
解决问题的思路和方法

## 6 所有用户消息
用户说过的所有非工具结果的话（原文保留！）

## 7 待办任务
还没完成的事

## 8 当前工作
最近在做什么（要最详细）

## 9 可能的下一步
接下来打算做什么

你必须不调用任何工具。输出纯文本。"""


# ── 对话序列化 ───────────────────────────────────────────────────


def serialize_conversation(msgs: list[Message]) -> str:
    """把对话扁平化成可读文本。

    不暴露 ToolCall.input 原 JSON（摘要不需要细节），
    只记录工具调用名称与参数摘要。

    Args:
        msgs: 内部 Message 列表。

    Returns:
        可读的对话文本，每条消息一行。
    """
    lines: list[str] = []
    for m in msgs:
        role = m.role.value
        content = m.content

        if isinstance(content, str):
            lines.append(f"{role}: {content}")
        elif isinstance(content, list):
            # 内容块列表
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        lines.append(f"{role}: {text}")
                elif btype == "tool_use":
                    name = block.get("name", "unknown")
                    tid = block.get("id", "")
                    inp = block.get("input", {})
                    inp_str = _summarize_input(inp)
                    lines.append(f"{role}: [call {name} id={tid} args={inp_str}]")
                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    result_content = block.get("content", "")
                    is_error = block.get("is_error", False)
                    preview = _preview_result(result_content)
                    err_mark = " [ERROR]" if is_error else ""
                    lines.append(f"user: [result id={tid}{err_mark}] {preview}")
    return "\n".join(lines)


def _summarize_input(inp: dict[str, Any]) -> str:
    """压缩工具调用参数为简短摘要。

    对于大参数值（如文件内容），截断到前 200 字符。
    """
    import json

    if not inp:
        return "{}"
    # 对大值做截断
    summarized = {}
    for k, v in inp.items():
        if isinstance(v, str) and len(v) > 200:
            summarized[k] = v[:200] + "..."
        else:
            summarized[k] = v
    return json.dumps(summarized, ensure_ascii=False)


def _preview_result(content: str | list[dict]) -> str:
    """生成工具结果的简短预览。

    用于序列化对话时减少 token 消耗（摘要 LLM 不需要完整结果）。
    截断到前 500 字符。
    """
    if isinstance(content, str):
        if len(content) > 500:
            return content[:500] + "..."
        return content
    # list[dict] 类型
    text_parts = []
    for item in content:
        if isinstance(item, dict) and "text" in item:
            text_parts.append(item["text"])
    combined = " ".join(text_parts)
    if len(combined) > 500:
        return combined[:500] + "..."
    return combined


# ── Prompt 构造与解析 ────────────────────────────────────────────


def build_summary_prompt(msgs: list[Message]) -> list[Message]:
    """构建摘要 Prompt 消息列表。

    返回长度为 1 的列表，仅一条 user 消息。
    被压缩对话通过 serialize_conversation 扁平化后嵌入。

    Args:
        msgs: 需要被摘要的对话消息。

    Returns:
        适合作为 LLM 请求的消息列表。
    """
    from conversation.message import Message as Msg
    from conversation.message import MessageRole

    serialized = serialize_conversation(msgs)
    full_content = (
        SUMMARY_INSTRUCTION
        + "\n\n[conversation]\n"
        + serialized
        + "\n\n你必须不调用任何工具。输出纯文本。"
    )
    # 返回单条 user 消息（调用方应设置 tools=None 发送）
    return [Msg(role=MessageRole.USER, content=full_content)]


def extract_summary(raw: str) -> str:
    """从模型返回的整段文本中提取 <summary>...</summary> 正文。

    <analysis> 部分直接丢弃。提取失败时返回原文 + logging.warning。

    Args:
        raw: LLM 完整响应文本。

    Returns:
        <summary> 标签内的正文，或原文（降级）。
    """
    import logging

    logger = logging.getLogger(__name__)

    matches = re.findall(r"<summary>(.*?)</summary>", raw, re.DOTALL)
    if matches:
        # 取最后一对标签的内容
        return matches[-1].strip()

    logger.warning("摘要响应中未找到 <summary> 标签，降级使用原文")
    return raw
