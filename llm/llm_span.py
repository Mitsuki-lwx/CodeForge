"""LLM 请求的 gen_ai.* OTel span 封装。

在每个 LLM 请求(Session.stream_chat)外包一个带 GenAI semantic conventions 的
span,让 Langfuse 等 LLM 观测平台能把它识别为一次 LLM 调用(prompt/completion/
token/成本),而不是普通 trace。

识别依据(OpenTelemetry GenAI SemConv / Langfuse):
  gen_ai.system                 provider 体系(openai/anthropic/…)
  gen_ai.request.model          本次请求模型名
  gen_ai.operation.name         高层操作名(chat/embed 等)
  gen_ai.usage.input_tokens     输入 token
  gen_ai.usage.output_tokens    输出 token

本模块只依赖 core.observability 的 no-op 门面:未启用时零开销、绝不改变调用语义。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from llm.stream_events import CompletionDone, StreamError, StreamEvent

_ATTR_SYSTEM = "gen_ai.system"
_ATTR_MODEL = "gen_ai.request.model"
_ATTR_OP = "gen_ai.operation.name"
_ATTR_PROVIDER = "gen_ai.provider.name"
_ATTR_IN = "gen_ai.usage.input_tokens"
_ATTR_OUT = "gen_ai.usage.output_tokens"
_ATTR_CACHE_READ = "gen_ai.usage.cache_read_input_tokens"
_ATTR_CACHE_CREATION = "gen_ai.usage.cache_creation_input_tokens"


def _system_name(protocol: str) -> str:
    """协议 → gen_ai.system 名。map 到语义规范的 provider 名。"""
    if protocol == "anthropic":
        return "anthropic"
    return "openai"  # openai 兼容端点(含 DeepSeek/Langfuse 网关)


def gen_ai_span(
    *,
    protocol: str,
    model: str,
    provider: str,
) -> Any:
    """开启一个 gen_ai.* LLM span。

    Returns:
        一个 context manager;需保证 finally 退出。内部通过
        core.observability.providers.get_tracer 拿 tracer(no-op 时返回
        等效 no-op 的 context manager,绝不抛)。
    """
    from contextlib import nullcontext

    from core.observability.providers import get_tracer

    tracer = get_tracer("codeforge.llm")
    start = getattr(tracer, "start_as_current_span", None)
    if start is None:
        # 可观测性未启用:get_tracer 返回 nullcontext,没有 start_as_current_span
        return _Exited(nullcontext(), _NullSpan())

    cm = start("chat.completions")
    span = cm.__enter__()
    if hasattr(span, "set_attribute"):
        span.set_attribute(_ATTR_OP, "chat")
        span.set_attribute(_ATTR_SYSTEM, _system_name(protocol))
        span.set_attribute(_ATTR_PROVIDER, provider)
        span.set_attribute(_ATTR_MODEL, model)
        _set_iteration_attrs(span)
        _set_agent_attrs(span)
    return _Exited(cm, span)


def _set_agent_attrs(span: Any) -> None:
    """把当前执行 agent 的身份（id + 可读名）写进 LLM span，供按 subagent 聚合。"""
    try:
        from core.observability.context import get_agent_identity

        identity = get_agent_identity()
    except Exception:  # noqa: BLE001 —— 观测辅助失败静默
        return
    if identity is None:
        return
    agent_id, agent_name = identity
    try:
        span.set_attribute("codeforge.agent.id", agent_id)
        if agent_name:
            span.set_attribute("codeforge.agent.name", agent_name)
    except Exception:  # noqa: BLE001 —— 观测失败静默
        return


def _set_iteration_attrs(span: Any) -> None:
    """把循环当前迭代元数据(迭代号 + 喂入 payload 字符数)写进 LLM span。"""
    try:
        from core.observability.context import get_iteration_meta

        meta = get_iteration_meta()
    except Exception:  # noqa: BLE001 —— 观测辅助失败静默
        return
    if meta is None:
        return
    iteration, context_chars = meta
    try:
        span.set_attribute("codeforge.iteration", iteration)
        span.set_attribute("codeforge.iteration_context_chars", context_chars)
    except Exception:  # noqa: BLE001
        pass


class _NullSpan:
    """no-op span 桩:所有方法空实现。"""

    def set_attribute(self, *a, **k):
        return None


class _Exited:
    """包住 span 的退出 cm,按事件收尾。"""

    def __init__(self, cm: Any, span: Any) -> None:
        self._cm = cm
        self._span = span

    def end_ok(self, event: CompletionDone) -> None:
        if hasattr(self._span, "set_attribute"):
            usage = event.usage or {}
            if usage.get("input_tokens") is not None:
                self._span.set_attribute(_ATTR_IN, usage["input_tokens"])
            if usage.get("output_tokens") is not None:
                self._span.set_attribute(_ATTR_OUT, usage["output_tokens"])
            # 缓存命中：让 Langfuse 里能看「稳定内容从缓存读、省了多少 token」
            if usage.get("cache_read_input_tokens"):
                self._span.set_attribute(
                    _ATTR_CACHE_READ, usage["cache_read_input_tokens"]
                )
            if usage.get("cache_creation_input_tokens"):
                self._span.set_attribute(
                    _ATTR_CACHE_CREATION, usage["cache_creation_input_tokens"]
                )

    def end_error(self, message: str) -> None:
        if hasattr(self._span, "set_status"):
            self._span.set_status(trace_status_error(), message)

    def close(self) -> None:
        try:
            self._cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 —— 观测失败绝不抛出
            pass


def trace_status_error():
    """返回 OpenTelemetry 的 StatusCode.ERROR(尽力,不在则返回 2/None)。"""
    try:
        from opentelemetry.trace import Status, StatusCode

        return Status(StatusCode.ERROR)
    except Exception:  # noqa: BLE001
        return None


async def wrap_events(
    events: AsyncGenerator[StreamEvent, None],
    span_ctx: Any,
) -> AsyncGenerator[StreamEvent, None]:
    """透传事件流,并在结尾/出错时给 span 补 usage 或 error 状态。"""
    saw_done = False
    try:
        async for ev in events:
            if isinstance(ev, CompletionDone):
                span_ctx.end_ok(ev)
                saw_done = True
            elif isinstance(ev, StreamError):
                span_ctx.end_error(ev.message or "")
            yield ev
    finally:
        if not saw_done:
            # 未到 CompletionDone 自然结束(被中断等),不额外标 error,仅正常关闭
            pass
        span_ctx.close()
