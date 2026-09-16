"""CodeForge 逐个 item 的执行引擎。

每个 dataset item 在一个全新临时目录里跑一个独立的 CodeForge Agent
（复用 scripts/run_trace_demo.py 的无头装配：PermissionMode.BYPASS、runtime=None、
tempfile cwd）。用 `run_to_completion` 拿最终文本。

关键点：在每次 run 外面包一个 per-item 的 OTel 根 span。CodeForge 内层的
chat.completions / tool.* span 都是对环境上下文 start_as_current_span，包根 span
后它们会自然嵌套其下，从而得到一个确定、逐 item 的 trace_id，用于显式关联
Langfuse experiment 的 run item。可观测性未启用时 get_tracer 返回 nullcontext，
根 span 与 trace_id 退化为 no-op（不阻塞）。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent import Agent, AgentConfig
from core.permissions.modes import PermissionMode
from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from llm.client import LLMClient

# 简短无头 system prompt，让 model 聚焦任务、最后给一句总结。
AGENT_SYSTEM_PROMPT = (
    "You are CodeForge, a terminal coding assistant. Use file tools to inspect and "
    "edit code in the current working directory. When done, reply with a short "
    "one-sentence summary of what you changed and the result."
)


def _open_item_span(item_name: str, session_id: str | None = None):
    """开一个 per-item OTel 根 span。可观测性未启用时退化 no-op。

    在根 span 上设 `langfuse.session.id`，让 Langfuse 的 OTLP property-mapping
    （https://langfuse.com/docs/opentelemetry#property-mapping）把同一次 benchmark
    的根 span 归到一个 Session —— 这样 Langfuse 的 Sessions 视图能看到「一整条
    run」（如 dev / full-1 / full-2），而非按 item 拆碎成 6 段。
    session_id 不在时只开 span、不设属性（后退成无 Session 的旧行为）。
    """
    from core.observability.providers import get_tracer

    tracer = get_tracer("codeforge.bench")
    start = getattr(tracer, "start_as_current_span", None)
    if start is None:
        return None, None  # no-op
    cm = start(f"bench:{item_name}")
    span = cm.__enter__()
    if span is not None and session_id:
        try:
            span.set_attribute("langfuse.session.id", session_id)
        except Exception:  # noqa: BLE001
            pass
    return cm, span


async def run_one(
    provider: ProviderConfig,
    item: dict,
    *,
    max_iterations: int = 12,
    keep_artifacts: bool = False,
    dataset_name: str = "",
    session_id: str | None = None,
    deny_tools: list[str] | None = None,
) -> dict[str, Any]:
    """跑一个 dataset item，返回结果 dict。

    Args:
        session_id: 挂在根 span 的 Langfuse session id（通常为 whole-run 的
            `bench:{variant}`），用于在 Sessions 视图对整条 run 归组；None 则不设。
        deny_tools: 可选；强制拒绝执行这些工具（边界评测用，默认 None 不拒绝）。

    Returns:
        {"name", "output", "usage", "elapsed_s", "trace_id", "tmpdir"}
        trace_id 在可观测性启用时为 32 位 hex，否则 None。
    """
    tmp = Path(tempfile.mkdtemp(prefix="codeforge_bench_"))
    for fname, content in (item.get("seed_files") or {}).items():
        (tmp / fname).write_text(content, encoding="utf-8")

    registry = get_default_registry()
    client = LLMClient.create(provider)
    # CodeForge 内部的 execution session（用于归档/审计，per-item）；与发往 Langfuse
    # 的 OTel 根 span session（整条 run 归组）是两回事，变量分开避免互相覆盖。
    exec_session_id = f"bench:{dataset_name}:{item.get('name','')}"
    exec_ctx = ExecutionContext(cwd=tmp, session_id=exec_session_id)
    conv = ConversationManager(system_prompt=AGENT_SYSTEM_PROMPT)
    agent = Agent(
        registry=registry,
        llm_client=client,
        exec_ctx=exec_ctx,
        conversation=conv,
        config=AgentConfig(max_iterations=max_iterations),
        runtime=None,
    )
    # 无头演示：跳过 HITL，权限检查直接判定
    agent.set_permission_mode(PermissionMode.BYPASS)
    if deny_tools:
        agent.set_deny_tools(deny_tools)

    cm, span = _open_item_span(item.get("name", "item"), session_id=session_id)

    trace_id = None
    if span is not None:
        try:
            trace_id = format(span.get_span_context().trace_id, "032x")
        except Exception:  # noqa: BLE001
            trace_id = None

    try:
        from core.observability.providers import snapshot as _snapshot
        _before = (_snapshot().get("codeforge.tool.calls") or 0)
    except Exception:  # noqa: BLE001
        _before = None

    try:
        start = time.monotonic()
        final_text = await agent.run_to_completion(conv, task=item.get("task", ""))
        elapsed = time.monotonic() - start
    finally:
        if cm is not None:
            cm.__exit__(None, None, None)

    tool_calls = None
    if _before is not None:
        try:
            _after = (_snapshot().get("codeforge.tool.calls") or 0)
            tool_calls = max(0, _after - _before)
        except Exception:  # noqa: BLE001
            tool_calls = None

    out: dict[str, Any] = {
        "name": item.get("name", ""),
        "output": final_text,
        "usage": dict(getattr(agent, "_total_usage", None) or {}),
        "elapsed_s": round(elapsed, 2),
        "tool_calls": tool_calls,
        "trace_id": trace_id,
        "tmpdir": str(tmp),
    }
    if not keep_artifacts:
        shutil.rmtree(tmp, ignore_errors=True)
    return out
