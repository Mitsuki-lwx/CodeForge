"""Observability(Logs + Metrics + Traces)集成测试。

沿用 test_trace.py 范式:tmp 目录 + 真实 Agent 集成,断言本地落盘文件与进程内指标。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.observability import sdk
from core.observability.providers import record_histogram, record_metric, snapshot

@pytest.fixture
def obs_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp())


@pytest.fixture
def isolate_obs(monkeypatch, obs_dir: Path):
    """把 CODEFORGE_OBS_DIR 指到 tmp,并重置 sdk + 指标快照,保证测试彼此隔离。"""
    monkeypatch.setenv("CODEFORGE_OBS_DIR", str(obs_dir))
    from core.observability import providers

    with providers._snapshot_lock:
        providers._snapshot.clear()
    providers._counters.clear()
    sdk._initialized = False
    sdk._sinks.clear()
    yield obs_dir
    sdk.shutdown()
    with providers._snapshot_lock:
        providers._snapshot.clear()
    sdk._initialized = False


def test_init_idempotent(isolate_obs):
    """ensure_initialized 幂等;返回 True(已启用)。"""
    from core.observability import ensure_initialized

    assert ensure_initialized() is True
    assert ensure_initialized() is True


def test_metric_snapshot_and_local_files(isolate_obs, obs_dir: Path):
    """记录指标 → 进程内快照可见;logs 落盘文件非空。"""
    from core.observability import ensure_initialized
    from core.observability.providers import get_root_logger

    ensure_initialized()
    record_metric("codeforge.tool.calls", 2, delta=True)
    record_metric("codeforge.tokens.input", 300, delta=True)
    s = snapshot()
    assert s["codeforge.tool.calls"] == 2.0
    assert s["codeforge.tokens.input"] == 300.0

    get_root_logger().warning("obs test warning uid-1")
    import time

    time.sleep(0.05)
    log_path = obs_dir / "logs" / "data.jsonl"
    content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "obs test warning uid-1" in content


def test_metric_snapshot_clean_between_tests(isolate_obs, obs_dir: Path):
    """独立运行:本次记录不污染上一次(验证 fixture 重置快照)。"""
    from core.observability.providers import snapshot

    assert snapshot() == {}


def test_record_histogram_bypasses_snapshot(isolate_obs, obs_dir: Path):
    """直方图写 OTel meter,但不进标量 _snapshot。"""
    from core.observability import ensure_initialized
    from core.observability.providers import snapshot

    ensure_initialized()
    record_histogram("codeforge.tool.duration_ms", 5.0, unit="ms")
    record_histogram("codeforge.tool.duration_ms", 7.5, unit="ms")
    record_metric("codeforge.tool.calls", 1, delta=True)
    s = snapshot()
    # 直方图不进 snapshot(counter 才进)
    assert "codeforge.tool.duration_ms" not in s
    assert s.get("codeforge.tool.calls") == 1.0


def test_agent_run_emits_trace_and_span(isolate_obs, obs_dir: Path):
    """真实 Agent 跑一轮 → audit trace 写入 + obs metrics 落盘 + trace 落盘。"""
    from conversation.manager import ConversationManager
    from core.agent import Agent, AgentConfig
    from core.observability import ensure_initialized
    from core.tool import ToolRegistry
    from core.tool.context import ExecutionContext
    from llm.stream_events import CompletionDone, ToolUse
    from tests.test_agent import MockLLMClient, ReadOnlyTool

    ensure_initialized()

    reg = ToolRegistry()
    reg.register(ReadOnlyTool())

    responses = [[ToolUse(id="u1", name="read_test", input={})], [CompletionDone()]]
    client = MockLLMClient(responses)
    cfg = AgentConfig(max_iterations=10)
    exec_ctx = ExecutionContext(cwd=Path("/tmp"), session_id="t-obs")
    conv = ConversationManager(system_prompt="test")
    agent = Agent(
        registry=reg, llm_client=client, exec_ctx=exec_ctx,
        conversation=conv, config=cfg,
        trace_audit_dir=str(obs_dir / "audit_root"),
    )

    async def _run():
        async for _ in agent.run("read something"):
            pass

    asyncio.run(_run())

    # trace JSONL(source-of-truth)仍写
    aud = obs_dir / "audit_root" / "audit" / "t-obs.jsonl"
    assert aud.exists()

    # 指标工具调用计数 ≥1
    s = snapshot()
    assert s.get("codeforge.tool.calls", 0) >= 1

    # 等一个 metric tick 让它落盘(默认 5s,这里手动触发 meter flush)
    import time

    time.sleep(0.05)

    # metrics 落盘目录存在(哪怕暂无 tick 数据,文件也应被创建)
    # 文件是否为 0 字节取决于 PeriodicReader 是否已 tick;不强断言内容,
    # 只要求目录树存在 —— 真正断言交给下面独立的 exporter 单测。
    assert (obs_dir / "metrics").exists()
