"""Agent 的可观测性出口 —— 审计 trace / 进程内指标 / 工具 span。

从 `core/agent/agent.py` 抽出的一簇方法。它们只读 Agent 的少数字段
（`_trace_writer` / `_trace_audit_dir` / `_exec_ctx` / `_agent_name`），
不参与 ReAct 循环的任何决策；混在主循环里只会让它更难读。

**为什么是 Mixin 而不是独立对象**：这 6 个方法在 Agent 内部有 26 处调用点，
改成 `self._telemetry.emit_metric(...)` 是纯粹的改名噪音，收益不抵风险。
Mixin 保持 `self.` 不变，行为逐字不变，只是从 agent.py 挪进本文件。

所有方法都是**尽力而为**：可观测性失败绝不抛出、绝不改变主流程语义。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

from core.tool.context import ExecutionContext

if TYPE_CHECKING:
    pass


class AgentIdentitySource(Protocol):
    """`stamp_agent_attrs` 需要的宿主最小面。

    `Agent` 与 `TelemetryMixin`（方法实际跑在 Mixin 上）都满足它 —— 用 Protocol
    而不是写死 `Agent`，是因为 Mixin 独立看时不是 `Agent` 的子类。
    """

    _exec_ctx: ExecutionContext
    _agent_name: str


def stamp_agent_attrs(span: Any, agent: AgentIdentitySource) -> None:
    """给工具 span 打当前 agent 身份（id + 可读名），供在 Langfuse 按 subagent 聚合。

    优先读 ContextVar（LLM 循环设的）；兜底用 agent 自身字段。尽力而为，绝不抛。
    """
    try:
        from core.observability.context import get_agent_identity

        identity = get_agent_identity()
        if identity is not None:
            agent_id, agent_name = identity
        else:
            agent_id = str(agent._exec_ctx.session_id)
            agent_name = getattr(agent, "_agent_name", "") or "lead"
        if hasattr(span, "set_attribute"):
            span.set_attribute("codeforge.agent.id", agent_id or "?")
            if agent_name:
                span.set_attribute("codeforge.agent.name", agent_name)
    except Exception:  # noqa: BLE001 —— 观测辅助失败静默
        return


class TelemetryMixin:
    """`Agent` 的 trace / metrics / span 发射口。

    ── 对宿主的依赖契约（由 `Agent.__init__` 赋值）────────────────────
    这里**声明**本 Mixin 用到的宿主属性。对运行时是零影响的（纯注解不创建属性），
    作用是：① mypy 能检查本文件；② 读代码时一眼看到耦合面有多大。
    """

    _exec_ctx: ExecutionContext
    _agent_name: str
    _trace_writer: Any
    _trace_audit_dir: Any

    def _get_trace_writer(self):
        """惰性取审计写入器;失败返回 None(采集失败绝不阻断主流程)。"""
        if self._trace_writer is None:
            try:
                from core.trace.writer import TraceWriter

                self._trace_writer = TraceWriter(
                    self._exec_ctx.session_id, audit_dir=self._trace_audit_dir
                )
            except Exception:  # noqa: BLE001 —— trace 初始化失败静默降级
                self._trace_writer = None
        return self._trace_writer

    def _trace_record(self, event) -> None:
        """发一条审计事件;writer 为空或写失败都静默跳过。"""
        writer = self._get_trace_writer()
        if writer is not None:
            try:
                writer.record(event)
            except Exception:  # noqa: BLE001 —— 审计失败绝不抛出
                pass

    def _trace_write_dict(self, data: dict) -> None:
        """发一条**已构造好的 dict** 行，自行补齐 session_id / ts / sequence。

        ★ 不要用 `_trace_record` 传裸 dict：`TraceWriter.record` 开头会访问
        `event.session_id`，裸 dict 没这个属性 ⇒ `AttributeError`
        ⇒ 被写路径的 `except: pass` **静默吞掉**，事件一行都不落。
        （实测踩过：文件存在但零行，看不出任何错误。）
        dict 入口是 `TraceWriter.write`。

        ★ `write()` 契约上**不注入** session/ts/sequence（见 writer.py docstring），
        所以这里必须自己补：`TraceWriter.record` 才做那套注入，走 dict 就绕过了。
        少 `ts` 尤其致命 —— 事后按时间聚合（"误判集中在哪几天/哪类表述"）
        就无从下手，埋点等于白埋。
        不设 `sequence`：不碰 writer 的私有 `_seq`；文件是 append-only，
        同一文件内的**行序**即稳定写入序，够排序用。
        """
        writer = self._get_trace_writer()
        if writer is not None:
            payload = {
                "session_id": self._exec_ctx.session_id,
                "ts": int(time.time() * 1000),
                **data,
            }
            try:
                writer.write(payload)
            except Exception:  # noqa: BLE001 —— 审计失败绝不抛出
                pass

    def _trace_close(self) -> None:
        """关闭审计写入器(幂等);未创建过 writer 或已关闭都静默。"""
        if self._trace_writer is not None:
            try:
                self._trace_writer.close()
            except Exception:  # noqa: BLE001 —— 关闭失败绝不抛出
                pass

    def _emit_metric(self, name: str, value: float, *, delta: bool = True) -> None:
        """发一个进程内指标(可观测性);未启用/失败静默。"""
        try:
            from core.observability.providers import record_metric

            record_metric(name, value, delta=delta)
        except Exception:  # noqa: BLE001 —— 可观测性失败绝不抛出
            pass

    def _emit_tool_span(
        self,
        tool_use_id: str,
        tool_name: str,
        success: bool,
        duration_ms: int,
        retries: int = 0,
        timed_out: bool = False,
    ) -> None:
        """为一次工具执行发一个 OTel span(尽力而为;失败静默)。

        用 tool_use_id 作 span 名,标记 tool_name/success/duration/retries/超时;
        失败时把 span.status 置为 ERROR(便于在 trace 树里一眼看出红点)。
        不嵌套父级(并发只读工具下父子关系由 trace JSONL 的 parent_span_id 承担)。
        """
        try:
            from core.observability.providers import get_tracer
            from llm.llm_span import trace_status_error

            tracer = get_tracer("codeforge.tools")
            start = getattr(tracer, "start_as_current_span", None)
            if start is None:
                return  # 可观测性未启用,直接跳过
            outer = start(f"tool.{tool_name}")
            span = outer.__enter__()
            try:
                if hasattr(span, "set_attribute"):
                    span.set_attribute("tool_use_id", tool_use_id)
                    span.set_attribute("success", success)
                    span.set_attribute("duration_ms", duration_ms)
                    span.set_attribute("codeforge.tool.retries", retries)
                    span.set_attribute("codeforge.tool.timeout", timed_out)
                    stamp_agent_attrs(span, self)
                if not success and hasattr(span, "set_status"):
                    span.set_status(trace_status_error(), None)
            finally:
                outer.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 —— 可观测性失败绝不抛出
            pass


__all__ = ["TelemetryMixin", "stamp_agent_attrs"]
