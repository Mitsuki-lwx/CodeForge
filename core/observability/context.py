"""迭代级上下文载具(ContextVar)。

在 ReAct 循环每次调用 LLM 前,把「本次迭代号 + 喂给模型的 payload 字符大小」
放进 ContextVar。llm_span 在开 chat.completions span 时读取,并写进 span 属性,
用于诊断「循环是否逐迭代膨胀(上下文爆炸)/迭代号定位」。

用 ContextVar 而非改 gen_ai_span/stream_chat 签名:asyncio Task 隔离(并行子
agent 各自隔离),主循环与子 agent 循环统一 set、session.py 一处 get,改动最小。
ContextVar 操作永不抛,天然 no-op。
"""

from __future__ import annotations

from contextvars import ContextVar

_iteration_meta: ContextVar[tuple[int, int] | None] = ContextVar(
    "codeforge.iteration_meta", default=None
)

# 当前执行 agent 的身份 (id, name)。id 用 _exec_ctx.session_id（唯一、含 -sub 后缀），
# name 用可读名（Agent 工具 name / 角色名 / 'sub' / 'lead'）。多智能体下区分
# 每个 span 属主，供在 Langfuse 按 agent 聚合。asyncio Task 隔离 + 传播。
_agent_identity: ContextVar[tuple[str, str] | None] = ContextVar(
    "codeforge.agent_identity", default=None
)


def set_agent_identity(agent_id: str | None, name: str | None) -> None:
    """在 agent 循环里设置当前执行者身份（id, 可读名）。id 缺省取 '?'。"""
    if agent_id is None and name is None:
        _agent_identity.set(None)
    else:
        _agent_identity.set((agent_id or "?", name or ""))


def get_agent_identity() -> tuple[str, str] | None:
    """span 发射点读取当前 agent 身份：(id, name)；无则为 None。"""
    return _agent_identity.get()


def reset_agent_identity() -> None:
    """清除当前 task 的 agent 身份。"""
    _agent_identity.set(None)


def set_iteration_meta(iteration: int | None, context_chars: int | None) -> None:
    """在每次 LLM 调用前调用:记录迭代号与本次 payload 字符数。"""
    if iteration is None and context_chars is None:
        _iteration_meta.set(None)
    else:
        _iteration_meta.set((iteration or 0, context_chars or 0))


def get_iteration_meta() -> tuple[int, int] | None:
    """llm_span 读取:(iteration, context_chars);无则为 None。"""
    return _iteration_meta.get()


def reset_iteration_meta() -> None:
    """清除当前 task 的迭代元数据(可选,避免泄漏到后续调用)。"""
    _iteration_meta.set(None)
