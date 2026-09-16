"""Trace 数据层(T1 schema + T2 writer)单测。

对应 docs/checklist_trace.md 的 Group A(数据层)与 Group B(持久性与解耦)
中 T1/T2 覆盖项。T3+ 的 record 语义、span 关联、事件覆盖留到后续任务。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.trace.events import (
    AgentEndEvent,
    CompactEvent,
    PermissionEvent,
    ToolEndEvent,
    ToolStartEvent,
    TraceEvent,
)
from core.trace.writer import TraceWriter


@pytest.fixture()
def audit_dir() -> Path:
    return Path(tempfile.mkdtemp())


def _read_lines(path: Path) -> list[dict]:
    """逐行读回 audit 文件,坏行不该出现(由本套测试断言其合法性)。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── Group A: 数据层 ─────────────────────────────────────────────

def test_to_dict_keys_ordered():
    """同类别事件 to_dict 键顺序稳定一致。"""
    a = ToolStartEvent(tool_use_id="t1", tool_name="read_file", ts=1)
    b = ToolStartEvent(tool_use_id="t2", tool_name="bash", ts=2)
    assert list(a.to_dict().keys()) == list(b.to_dict().keys())


def test_empty_optional_fields_omitted():
    """可空字段(duration_ms/token/parent_span_id)为空时不出现在行里。"""
    d = ToolStartEvent(tool_use_id="t1", tool_name="read_file", ts=1).to_dict()
    assert "duration_ms" not in d
    assert "token" not in d
    assert "parent_span_id" not in d
    assert d["event"] == "tool_start"


def test_populated_optional_fields_present():
    """填了 duration_ms/token 的事件保留这些字段。"""
    d = ToolEndEvent(
        tool_use_id="t1", tool_name="read_file", success=True,
        duration_ms=15, ts=3,
    ).to_dict()
    assert d["duration_ms"] == 15
    assert "result_preview" not in d  # 空预览省略


def test_writer_writes_valid_json_lines(audit_dir):
    """连续写多类事件,每行都是合法 JSON,行数==事件数。"""
    w = TraceWriter("s_a", audit_dir=audit_dir)
    w.record(ToolStartEvent(tool_use_id="t1", tool_name="read_file", ts=1))
    w.record(PermissionEvent(
        tool_use_id="t2", tool_name="bash", decision="deny",
        reason="Not allowed", ts=2,
    ))
    w.record(ToolEndEvent(
        tool_use_id="t1", tool_name="read_file", success=True,
        duration_ms=12, ts=3,
    ))
    w.close()

    lines = _read_lines(w.path)
    assert len(lines) == 3
    assert lines[0]["event"] == "tool_start"
    assert lines[1]["decision"] == "deny"
    assert lines[2]["event"] == "tool_end"


def test_sequence_monotonic(audit_dir):
    """同一会话内 sequence 单调递增且无重复。"""
    w = TraceWriter("s_seq", audit_dir=audit_dir)
    for i in range(5):
        w.record(ToolStartEvent(tool_use_id=f"t{i}", tool_name="read_file", ts=i))
    w.close()

    seqs = [e["sequence"] for e in _read_lines(w.path)]
    assert seqs == [1, 2, 3, 4, 5]
    assert len(set(seqs)) == 5


def test_session_id_injected(audit_dir):
    """record 自动注入 session_id(未显式给时)。"""
    w = TraceWriter("s_id_inject", audit_dir=audit_dir)
    w.record(ToolStartEvent(tool_use_id="t1", tool_name="read_file", ts=1))
    w.close()
    lines = _read_lines(w.path)
    assert lines[0]["session_id"] == "s_id_inject"


def test_write_resumes_from_tail_not_truncate(audit_dir):
    """重开同会话不清空原内容,且 sequence 从 max+1 续写。"""
    w1 = TraceWriter("s_resume", audit_dir=audit_dir)
    w1.record(ToolStartEvent(tool_use_id="t1", tool_name="read_file", ts=1))
    w1.record(ToolStartEvent(tool_use_id="t2", tool_name="bash", ts=2))
    w1.close()

    w2 = TraceWriter("s_resume", audit_dir=audit_dir)
    w2.record(ToolStartEvent(tool_use_id="t3", tool_name="grep_tool", ts=3))
    w2.close()

    lines = _read_lines(w2.path)
    assert len(lines) == 3
    assert lines[2]["sequence"] == 3  # 从原 max(2)+1 续


def test_audit_dir_under_given_root(audit_dir):
    """audit/ 目录在指定根下,文件名为 <session>.jsonl。"""
    w = TraceWriter("s_path", audit_dir=audit_dir)
    w.close()
    assert w.path == audit_dir / "audit" / "s_path.jsonl"


# ── Group B: 持久性与解耦(数据层部分) ─────────────────────────

def test_write_failure_does_not_raise(audit_dir):
    """关闭后 record 静默失败,不抛出(调用方不阻断)。"""
    w = TraceWriter("s_closed", audit_dir=audit_dir)
    w.close()
    # 不应抛
    w.record(ToolStartEvent(tool_use_id="t1", tool_name="read_file", ts=1))


def test_unicode_not_escaped(audit_dir):
    """中文不被 \\u 转义(ensure_ascii=False)。"""
    w = TraceWriter("s_cjk", audit_dir=audit_dir)
    w.record(PermissionEvent(
        tool_use_id="t1", tool_name="bash", decision="deny",
        reason="无权限执行该命令", ts=1,
    ))
    w.close()
    raw = w.path.read_text(encoding="utf-8")
    assert "\\u" not in raw
    assert "无权限执行该命令" in raw


def test_write_dict_compatible(audit_dir):
    """write({...}) 兼容 dict 输入,直接落行。"""
    w = TraceWriter("s_dict", audit_dir=audit_dir)
    w.write({"event": "mock", "k": "v"})
    w.close()
    lines = _read_lines(w.path)
    assert lines[0]["event"] == "mock"
    assert lines[0]["k"] == "v"


# ── 资产:7 类事件都可达(防 import 缺类) ─────────────────────

def test_all_event_classes_importable():
    """7 类事件常量存在且 event 值唯一。"""
    cls = [
        ToolStartEvent, ToolEndEvent, PermissionEvent,
        CompactEvent, AgentEndEvent,
    ]
    # AgentErrorEvent 经 __init__ 导出
    from core.trace import AgentErrorEvent
    cls.append(AgentErrorEvent)
    from core.trace import HookEvent
    cls.append(HookEvent)

    events = [c().event for c in cls]
    assert len(set(events)) == len(events)  # 不重复
    assert set(events) == {
        "tool_start", "tool_end", "permission", "hook", "compact",
        "agent_error", "agent_end",
    }


# ── T4/T5 集成:Agent 真实路径产出 audit 事件 ──────────────────────

def test_agent_read_tool_writes_trace(audit_dir):
    """只读工具执行 → audit 含 permission(allow) + tool_start/tool_end 成对。"""
    import asyncio

    from config.model import ProviderConfig
    from conversation.manager import ConversationManager
    from core.agent import Agent, AgentConfig
    from core.tool import ToolRegistry
    from core.tool.context import ExecutionContext
    from llm.stream_events import CompletionDone, ToolUse
    from tests.test_agent import MockLLMClient, ReadOnlyTool

    reg = ToolRegistry()
    reg.register(ReadOnlyTool())  # name=read_test

    # 模型第一轮请求调用 read_test 只读工具,第二轮纯文本结束
    responses = [[ToolUse(id="u1", name="read_test", input={})], [CompletionDone()]]
    client = MockLLMClient(responses)
    cfg = AgentConfig(max_iterations=10)
    exec_ctx = ExecutionContext(cwd=Path("/tmp"), session_id="t-read")
    conv = ConversationManager(system_prompt="test")
    agent = Agent(
        registry=reg, llm_client=client, exec_ctx=exec_ctx,
        conversation=conv, config=cfg, trace_audit_dir=str(audit_dir),
    )

    async def _run():
        async for _ in agent.run("read something"):
            pass

    asyncio.run(_run())

    path = audit_dir / "audit" / "t-read.jsonl"
    assert path.exists(), f"audit file missing: {path}"
    lines = _read_lines(path)
    starts = [e for e in lines if e["event"] == "tool_start"]
    ends = [e for e in lines if e["event"] == "tool_end"]
    perms = [e for e in lines if e["event"] == "permission"]

    # 只读工具放行 → permission allow
    assert any(p["decision"] == "allow" for p in perms), perms
    # tool 成对
    assert starts, "no tool_start"
    assert len(starts) == len(ends)
    # tool_end 带正耗时
    assert ends[0]["duration_ms"] >= 0
    assert ends[0]["success"] is True


def test_agent_dangerous_bash_denied(audit_dir):
    """危险命令 → audit 出现 permission deny 且 reason 非空。"""
    import asyncio

    from conversation.manager import ConversationManager
    from core.agent import Agent, AgentConfig
    from core.tool import ToolRegistry
    from core.tool.context import ExecutionContext
    from core.tool.tools.bash import BashTool
    from llm.stream_events import CompletionDone, ToolUse
    from tests.test_agent import MockLLMClient

    reg = ToolRegistry()
    reg.register(BashTool())

    responses = [
        [ToolUse(id="u1", name="bash", input={"command": "rm -rf /tmp/x"})],
        [CompletionDone()],
    ]
    client = MockLLMClient(responses)
    cfg = AgentConfig(max_iterations=10)
    exec_ctx = ExecutionContext(cwd=Path("/tmp"), session_id="t-deny")
    conv = ConversationManager(system_prompt="test")
    agent = Agent(
        registry=reg, llm_client=client, exec_ctx=exec_ctx,
        conversation=conv, config=cfg, trace_audit_dir=str(audit_dir),
    )

    async def _run():
        async for _ in agent.run("run this"):
            pass

    asyncio.run(_run())

    path = audit_dir / "audit" / "t-deny.jsonl"
    assert path.exists()
    lines = _read_lines(path)
    denies = [e for e in lines if e["event"] == "permission" and e["decision"] == "deny"]
    assert denies, f"no denied permission: {lines}"
    assert denies[0]["reason"], "deny reason empty"
    # 被拒工具不应产生 tool_start/tool_end
    assert not any(e["event"] == "tool_end" for e in lines)


# ── T7: end/error/compact 事件 ───────────────────────────────

def test_agent_natural_finish_records_end(audit_dir):
    """自然完成(纯文本回复)→ audit 出现 agent_end 事件。"""
    import asyncio

    from conversation.manager import ConversationManager
    from core.agent import Agent, AgentConfig
    from core.tool import ToolRegistry
    from core.tool.context import ExecutionContext
    from llm.stream_events import CompletionDone, TextChunk
    from tests.test_agent import MockLLMClient, ReadOnlyTool

    reg = ToolRegistry()
    reg.register(ReadOnlyTool())
    responses = [[TextChunk("done"), CompletionDone()]]
    client = MockLLMClient(responses)
    agent = Agent(
        registry=reg, llm_client=client,
        exec_ctx=ExecutionContext(cwd=Path("/tmp"), session_id="t-end"),
        conversation=ConversationManager(system_prompt="t"),
        config=AgentConfig(max_iterations=10),
        trace_audit_dir=str(audit_dir),
    )

    async def _run():
        async for _ in agent.run("hi"):
            pass

    asyncio.run(_run())

    path = audit_dir / "audit" / "t-end.jsonl"
    assert path.exists()
    ends = [e for e in _read_lines(path) if e["event"] == "agent_end"]
    assert ends, "no agent_end event"
    assert "elapsed_s" in ends[0]
    assert "token" in ends[0]  # agent_end 带总用量 dict


def test_agent_error_records_error_event(audit_dir):
    """stream_error → audit 出现 agent_error(code=stream_error)。"""
    import asyncio

    from conversation.manager import ConversationManager
    from core.agent import Agent, AgentConfig
    from core.tool import ToolRegistry
    from core.tool.context import ExecutionContext
    from llm.stream_events import StreamError
    from tests.test_agent import MockLLMClient, ReadOnlyTool

    reg = ToolRegistry()
    reg.register(ReadOnlyTool())
    responses = [[StreamError(message="boom")]]
    client = MockLLMClient(responses)
    agent = Agent(
        registry=reg, llm_client=client,
        exec_ctx=ExecutionContext(cwd=Path("/tmp"), session_id="t-err"),
        conversation=ConversationManager(system_prompt="t"),
        config=AgentConfig(max_iterations=10),
        trace_audit_dir=str(audit_dir),
    )

    async def _run():
        async for _ in agent.run("hi"):
            pass

    asyncio.run(_run())

    path = audit_dir / "audit" / "t-err.jsonl"
    assert path.exists()
    errs = [e for e in _read_lines(path) if e["event"] == "agent_error"]
    assert errs, "no agent_error event"
    assert errs[0]["code"] == "stream_error"


# ── T8: reader 读取/导出 ───────────────────────────────────

def test_reader_summary_counts(audit_dir):
    """session_summary 正确聚合事件数/拒绝数。"""
    w = TraceWriter("s_sum", audit_dir=audit_dir)
    w.record(ToolStartEvent(tool_use_id="t1", tool_name="bash", ts=1))
    w.record(PermissionEvent(
        tool_use_id="t2", tool_name="bash", decision="deny", reason="No", ts=2,
    ))
    w.record(ToolEndEvent(
        tool_use_id="t1", tool_name="bash", success=True, duration_ms=20, ts=3,
    ))
    w.close()

    from core.trace.reader import iter_session, session_summary
    summary = session_summary("s_sum", audit_dir=audit_dir)
    assert summary["events"] == 3
    assert summary["tools"] == 1
    assert summary["tool_duration_ms"] == 20
    assert summary["denied"] == 1
    # iter_session 逐行读回
    assert [e["event"] for e in iter_session("s_sum", audit_dir)] == [
        "tool_start", "permission", "tool_end",
    ]


def test_reader_summary_missing_session(audit_dir):
    """会话文件不存在 → 返回空计数摘要,不抛。"""
    from core.trace.reader import session_summary
    s = session_summary("no_such", audit_dir=audit_dir)
    assert s["events"] == 0
    assert s["tools"] == 0
    assert s["denied"] == 0


def test_reader_children_of(audit_dir):
    """children_of 按 parent_span_id 过滤。"""
    w = TraceWriter("s_children", audit_dir=audit_dir)
    w.record(ToolStartEvent(tool_use_id="a", tool_name="x", ts=1))
    w.record(ToolStartEvent(tool_use_id="b1", tool_name="y", parent_span_id="P1", ts=2))
    w.record(ToolStartEvent(tool_use_id="b2", tool_name="z", parent_span_id="P1", ts=3))
    w.close()

    from core.trace.reader import children_of
    kids = children_of("P1", "s_children", audit_dir=audit_dir)
    assert [e["tool_use_id"] for e in kids] == ["b1", "b2"]
    top = children_of("", "s_children", audit_dir=audit_dir)
    assert [e["tool_use_id"] for e in top] == ["a"]


# ── T9: writer 生命周期收敛(TraceWriter 层面验证 close 幂等性) ──

def test_writer_close_idempotent(audit_dir):
    """close 幂等;close 后 record 静默不抛。"""
    w = TraceWriter("s_cl", audit_dir=audit_dir)
    w.record(ToolStartEvent(tool_use_id="t1", tool_name="read_file", ts=1))
    w.close()
    w.close()  # 二次 close 不抛
    w.record(ToolStartEvent(tool_use_id="t2", tool_name="read_file", ts=2))  # close 后静默
    w.close()
    # 只有第一条被写入
    assert len(_read_lines(w.path)) == 1

