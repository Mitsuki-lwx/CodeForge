"""记忆更新器。

每 N 轮或用户显式记忆请求时，后台异步调 LLM 生成结构化笔记操作
（create / update / delete），执行后写入对应级别 memory 目录。
去重交给 LLM 判断；更新失败静默记录日志，不重试、不影响主会话。
"""

from __future__ import annotations

import json
import logging
import re

from conversation.message import APIMessage, Message
from llm.client import LLMClient
from llm.stream_events import CompletionDone, StreamError, TextChunk

logger = logging.getLogger(__name__)

# 每 N 轮自动触发记忆更新
MEMORY_AUTO_TURNS = 5

# 显式记忆请求关键词（大小写不敏感）
MEMORY_KEYWORDS = ("记住", "记忆", "别忘", "remember", "memo")

_MEMORY_INSTRUCTION = """你是 CodeForge 的记忆管理助手。根据「当前记忆索引」与「最近对话」，决定需要新增、更新还是删除哪些笔记。

笔记类型（type）与存放级别（level）：
- user_preference 用户偏好 / correction_feedback 纠正反馈 → level 为 "user"
- project_knowledge 项目知识 / reference_material 参考资料 → level 为 "project"

返回一个 JSON 数组，每个元素是一个操作：
- 新增：{"action":"create","level":"project","type":"project_knowledge","title":"...","slug":"...","content":"..."}
  slug 全小写下划线，如 api_conventions
- 更新：{"action":"update","level":"user","filename":"user_preference_terse.md","title":"...","content":"..."}
- 删除：{"action":"delete","level":"project","filename":"project_knowledge_old.md"}
不需要任何变更时返回 []。

去重与合并由你判断：索引中已有相似内容就更新或跳过，不要重复创建。
只输出 JSON，不要其他文字。

## 当前记忆索引
{index}

## 最近对话
{conversation}
"""


def should_trigger_memory(turn_count: int, user_input: str) -> bool:
    """每 MEMORY_AUTO_TURNS 轮，或用户消息含记忆关键词时触发（F35）。"""
    if turn_count > 0 and turn_count % MEMORY_AUTO_TURNS == 0:
        return True
    lowered = (user_input or "").lower()
    return any(kw in lowered for kw in MEMORY_KEYWORDS)


def _extract_json_array(text: str) -> list[dict]:
    """从模型输出提取 JSON 数组；失败返回空列表。"""
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return []


async def _request_ops(provider_config, index: str, conversation: str) -> list[dict]:
    """发记忆更新请求（tools=None），返回操作列表。"""
    client = LLMClient.create(provider_config)
    prompt = _MEMORY_INSTRUCTION.replace("{index}", index or "(无)").replace(
        "{conversation}", conversation or "(无)"
    )
    api_msgs = [APIMessage(role="user", content=prompt)]

    text_buf: list[str] = []
    async for event in client.stream_chat(
        messages=api_msgs, system_prompt="", tools=None
    ):
        if isinstance(event, TextChunk):
            text_buf.append(event.text)
        elif isinstance(event, StreamError):
            raise RuntimeError(event.message)  # noqa: TRY004 —— 流错误用运行时异常上抛
        elif isinstance(event, CompletionDone):
            break
    return _extract_json_array("".join(text_buf))


def _apply_ops(store, ops: list[dict]) -> None:
    """执行笔记操作；单条失败仅告警不影响其余。"""
    for op in ops:
        action = op.get("action")
        level = op.get("level", "project")
        try:
            if action == "create":
                store.create_note(
                    level,
                    op.get("type", ""),
                    op.get("title", ""),
                    op.get("slug", ""),
                    op.get("content", ""),
                )
            elif action == "update":
                store.update_note(
                    level,
                    op.get("filename", ""),
                    op.get("title", ""),
                    op.get("content", ""),
                )
            elif action == "delete":
                store.delete_note(level, op.get("filename", ""))
            else:
                logger.warning("未知记忆操作: %s", action)
        except Exception as e:  # noqa: BLE001 —— 单条失败静默
            logger.warning("记忆操作失败 %s: %s", action, e)


async def update_memory(provider_config, store, recent_messages: list[Message]) -> None:
    """后台执行一次记忆更新（F36-F42）。

    只读 recent_messages 快照、只写 memory 目录；任何失败仅告警。
    """
    try:
        from core.context_compression.summary_prompt import serialize_conversation

        index = store.full_index()
        conversation = serialize_conversation(recent_messages or [])
        ops = await _request_ops(provider_config, index, conversation)
        if ops:
            _apply_ops(store, ops)
    except Exception:
        logger.exception("记忆更新失败")
