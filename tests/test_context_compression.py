"""上下文压缩子包单元测试。

覆盖 state / token / layer1 / summary_prompt / recovery / layer2 /
compact 编排 / config / conversation.replace_history。
manage_context 等需要 LLM 的路径通过 monkeypatch LLMClient.create 注入假客户端。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import pytest

from config.model import ProviderConfig
from config.protocol_defaults import effective_context_window
from conversation.manager import ConversationManager
from conversation.message import (
    Message,
    MessageRole,
    MessageStatus,
    make_tool_result_block,
)
from core.context_compression import (
    CompactCircuitBreaker,
    ContentReplacementState,
    FileReadRecord,
    ManageInput,
    ManageOutput,
    RecoveryState,
    SessionContext,
    TriggerKind,
    layer2,
    manage_context,
    new_session_context,
)
from core.context_compression.const import (
    MESSAGE_AGGREGATE_LIMIT,
    RECENT_KEEP_MESSAGES,
    SINGLE_RESULT_LIMIT,
)
from core.context_compression.layer1 import (
    build_preview,
    offload_and_snip,
    spill_single,
)
from core.context_compression.layer2 import (
    _join_after_summary,
    group_by_user_turn,
    pick_recent_tail,
)
from core.context_compression.recovery import build_recovery_attachment
from core.context_compression.state import _new_session_id
from core.context_compression.summary_prompt import (
    build_summary_prompt,
    extract_summary,
    serialize_conversation,
)
from core.context_compression.token import estimate_tokens, message_chars, usage_anchor
from llm import PromptTooLongError
from llm.client import LLMClient
from llm.stream_events import CompletionDone, TextChunk

PROVIDER = ProviderConfig(
    name="mock", protocol="anthropic", model="mock", api_key="mock"
)


# ── 测试辅助 ──────────────────────────────────────────────────────


def user(content: str) -> Message:
    return Message(
        role=MessageRole.USER, content=content, status=MessageStatus.COMPLETED
    )


def assistant(content: str) -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content=content,
        status=MessageStatus.COMPLETED,
    )


def user_tool_result(tid: str, content: str) -> Message:
    return Message(
        role=MessageRole.USER,
        content=[make_tool_result_block(tid, content)],
        status=MessageStatus.COMPLETED,
    )


def user_multi_results(items: list[tuple[str, str]]) -> Message:
    return Message(
        role=MessageRole.USER,
        content=[make_tool_result_block(tid, c) for tid, c in items],
        status=MessageStatus.COMPLETED,
    )


def make_session(tmp_path: Path) -> SessionContext:
    spill = tmp_path / "spill"
    spill.mkdir(parents=True, exist_ok=True)
    return SessionContext(session_id="test-session", spill_dir=str(spill))


class FakeStreamingClient(LLMClient):
    """可编程假客户端，模拟摘要请求响应序列。

    script 每步取值：
      "ok:<text>" — 返回该文本（含 <summary> 标签）
      "ptl"       — raise PromptTooLongError
      "err"       — raise RuntimeError
    """

    def __init__(self, script: list[str]):
        super().__init__(PROVIDER)
        self.script = script
        self.call_count = 0
        self.tools_calls: list = []
        self.messages_calls: list = []

    async def stream_chat(
        self, messages, system_prompt="", tools=None, system_blocks=None
    ):
        self.call_count += 1
        self.tools_calls.append(tools)
        self.messages_calls.append(messages)
        step = self.script[min(self.call_count - 1, len(self.script) - 1)]
        if step == "ptl":
            raise PromptTooLongError("prompt is too long")
        if step == "err":
            raise RuntimeError("provider 500")
        text = step.removeprefix("ok:")
        yield TextChunk(text=text)
        yield CompletionDone()


def patch_create(monkeypatch: pytest.MonkeyPatch, client: LLMClient) -> None:
    monkeypatch.setattr(LLMClient, "create", lambda cfg: client)


# ── 包结构 ────────────────────────────────────────────────────────


def test_package_exports():
    from core.context_compression import (
        ManageInput,
        TriggerKind,
        manage_context,
    )

    assert manage_context is not None
    assert TriggerKind.AUTO.value == "auto"
    assert ManageInput is not None and ManageOutput is not None


# ── SessionContext / new_session_context ───────────────────────────


def test_new_session_context_creates_spill_dir(tmp_path):
    ctx = new_session_context(str(tmp_path))
    # 新格式：YYYYMMDD-HHMMSS-xxxx
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", ctx.session_id)
    assert Path(ctx.spill_dir).is_dir()
    assert Path(ctx.session_dir).is_dir()
    # spill_dir 是 session_dir 的子目录
    assert str(Path(ctx.session_dir) / "tool-results") == ctx.spill_dir


def test_new_session_context_unique():
    a = _new_session_id()
    b = _new_session_id()
    assert a != b


# ── ContentReplacementState ────────────────────────────────────────


def test_decide_once_kept_freezes():
    state = ContentReplacementState()
    calls = []

    def decide():
        calls.append(1)
        return ("kept", "")

    assert state.decide_once("id1", "orig", decide) == "orig"
    assert state.decide_once("id1", "orig", decide) == "orig"
    assert len(calls) == 1  # 第二次不调回调


def test_decide_once_replaced_freezes_preview():
    state = ContentReplacementState()
    calls = []

    def decide():
        calls.append(1)
        return ("replaced", "PREVIEW")

    assert state.decide_once("id1", "orig", decide) == "PREVIEW"
    assert state.decide_once("id1", "orig", decide) == "PREVIEW"
    assert len(calls) == 1


def test_decide_once_skip_not_written():
    state = ContentReplacementState()
    calls = []

    def decide():
        calls.append(1)
        return ("skip", "")

    assert state.decide_once("id1", "orig", decide) == "orig"
    assert state.decide_once("id1", "orig", decide) == "orig"
    assert len(calls) == 2  # skip 不写账本，下次重走回调
    assert "id1" not in state._seen_ids


# ── CompactCircuitBreaker ──────────────────────────────────────────


def test_breaker_trips_after_3_failures():
    cb = CompactCircuitBreaker()
    cb.record_failure()
    cb.record_failure()
    assert not cb.tripped()
    cb.record_failure()
    assert cb.tripped()


def test_breaker_success_resets():
    cb = CompactCircuitBreaker()
    for _ in range(3):
        cb.record_failure()
    cb.record_success()
    assert not cb.tripped()


# ── RecoveryState ──────────────────────────────────────────────────


def test_recovery_snapshot_reverse_time():
    r = RecoveryState()
    r.record_file("a.txt", "A")
    r.record_file("b.txt", "B")
    # 显式覆写时间戳，避免同微秒导致顺序不稳定（naive 时间，仅排序用）
    r._files[str(Path("a.txt").resolve())].timestamp = datetime(  # noqa: DTZ001
        2026, 1, 1, 0, 0, 1
    )
    r._files[str(Path("b.txt").resolve())].timestamp = datetime(  # noqa: DTZ001
        2026, 1, 1, 0, 0, 2
    )
    snap = r.snapshot()
    assert [rec.path for rec in snap] == [
        str(Path("b.txt").resolve()),
        str(Path("a.txt").resolve()),
    ]


def test_recovery_snapshot_copy_isolation():
    r = RecoveryState()
    r.record_file("a.txt", "A")
    snap = r.snapshot()
    snap.clear()
    assert len(r.snapshot()) == 1


def test_recovery_resolves_relative_path():
    r = RecoveryState()
    r.record_file("x.txt", "X")
    resolved = str(Path("x.txt").resolve())
    assert resolved in r._files
    assert r.snapshot()[0].path == resolved


# ── Token 估算 ─────────────────────────────────────────────────────


def test_estimate_tokens_empty():
    assert estimate_tokens(0, [], 0) == 0


def test_estimate_tokens_anchor_plus_chars():
    msg = user("x" * 350)
    assert estimate_tokens(5000, [msg], 0) == 5000 + 100


def test_estimate_tokens_skips_before_anchor():
    m1 = user("x" * 700)
    m2 = user("y" * 350)
    assert estimate_tokens(5000, [m1, m2], 1) == 5000 + 100


def test_usage_anchor_merges_fields():
    u = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 20,
    }
    assert usage_anchor(u) == 200


def test_message_chars_counts_blocks():
    msgs = [user_tool_result("t1", "x" * 100)]
    assert message_chars(msgs) == 100


# ── Layer1 ─────────────────────────────────────────────────────────


def test_spill_single_idempotent(tmp_path):
    ctx = make_session(tmp_path)
    spill_single(ctx, "abc123", "hello")
    path = Path(ctx.spill_dir) / "abc123"
    mtime = path.stat().st_mtime_ns
    spill_single(ctx, "abc123", "hello")
    assert path.stat().st_mtime_ns == mtime


def test_offload_and_snip_single_over_limit(tmp_path):
    ctx = make_session(tmp_path)
    big = "A" * (SINGLE_RESULT_LIMIT + 1)
    msgs = [user_tool_result("tid1", big)]
    state = ContentReplacementState()
    out = offload_and_snip(msgs, state, ctx)

    preview = out[0].content[0]["content"]
    assert "original size:" in preview
    assert (Path(ctx.spill_dir) / "tid1").exists()
    assert state._seen_ids == {"tid1"}


def test_preview_has_stable_markers(tmp_path):
    ctx = make_session(tmp_path)
    big = "A" * (SINGLE_RESULT_LIMIT + 1)
    out = offload_and_snip(
        [user_tool_result("tid1", big)], ContentReplacementState(), ctx
    )
    preview = out[0].content[0]["content"]
    assert "original size:" in preview
    assert "tool-results" in preview or "spill" in preview
    assert "head preview" in preview
    assert "文件读取工具" in preview
    assert "不要凭头部预览猜测" in preview


def test_preview_head_bounds(tmp_path):
    ctx = make_session(tmp_path)
    # 每行 ~1200 字节 × 50 行 ≈ 60KB > 单条阈值 50K，确保触发落盘
    big = "\n".join(f"line {i} " + "x" * 1200 for i in range(50))
    out = offload_and_snip(
        [user_tool_result("t1", big)], ContentReplacementState(), ctx
    )
    preview = out[0].content[0]["content"]
    head = preview.split("[head preview]", 1)[1].split("完整内容", 1)[0].strip()
    lines = head.splitlines()
    assert len(lines) <= 20
    assert len(head.encode("utf-8")) <= 2048


def test_build_preview_deterministic(tmp_path):
    ctx = make_session(tmp_path)
    head = "H" * 100
    a = build_preview(1000, head, str(Path(ctx.spill_dir) / "x"))
    b = build_preview(1000, head, str(Path(ctx.spill_dir) / "x"))
    assert a == b


def test_offload_and_snip_aggregate_limit(tmp_path):
    ctx = make_session(tmp_path)
    # 同一条消息 6 条 50,000（恰好等于单条阈值，不触发单条）→ 聚合 300,000 > 200,000
    msgs = [user_multi_results([(f"t{i}", "B" * 50000) for i in range(6)])]
    state = ContentReplacementState()
    out = offload_and_snip(msgs, state, ctx)

    remaining = 0
    replaced = 0
    for block in out[0].content:
        c = block["content"]
        if "original size:" in c:
            replaced += 1
        else:
            remaining += len(c)
    assert replaced >= 2
    assert remaining <= MESSAGE_AGGREGATE_LIMIT


def test_offload_and_snip_decision_freeze(tmp_path):
    ctx = make_session(tmp_path)
    big = "A" * (SINGLE_RESULT_LIMIT + 1)
    msgs = [user_tool_result("tid1", big)]
    state = ContentReplacementState()
    out1 = offload_and_snip(msgs, state, ctx)
    out2 = offload_and_snip(msgs, state, ctx)
    assert out1[0].content[0]["content"] == out2[0].content[0]["content"]


def test_offload_and_snip_spill_failure_keeps_original(tmp_path):
    # spill_dir 指向一个"文件"，令 Path(file)/tid 抛 NotADirectoryError
    fake_dir = tmp_path / "not_a_dir"
    fake_dir.write_text("")
    ctx = SessionContext(session_id="s", spill_dir=str(fake_dir))
    big = "A" * (SINGLE_RESULT_LIMIT + 1)
    msgs = [user_tool_result("tid1", big)]
    state = ContentReplacementState()
    out = offload_and_snip(msgs, state, ctx)

    assert out[0].content[0]["content"] == big  # 保持原文
    assert "tid1" not in state._seen_ids  # 账本未写入


# ── 摘要 Prompt ────────────────────────────────────────────────────


def test_build_summary_prompt_shape():
    prompt = build_summary_prompt([user("hello")])
    assert len(prompt) == 1
    assert prompt[0].role.value == "user"


def test_summary_prompt_bookend_no_tool():
    prompt = build_summary_prompt([user("hello")])
    content = prompt[0].content
    assert content.startswith("你必须不调用任何工具")
    assert content.endswith("你必须不调用任何工具。输出纯文本。")


def test_summary_prompt_contains_tags_and_sections():
    prompt = build_summary_prompt([user("hello")])
    content = prompt[0].content
    assert "<analysis>" in content
    assert "<summary>" in content
    for i, title in enumerate(
        [
            "主要请求和意图",
            "关键技术概念",
            "文件和代码段",
            "错误和修复",
            "问题解决过程",
            "所有用户消息",
            "待办任务",
            "当前工作",
            "可能的下一步",
        ],
        start=1,
    ):
        assert f"## {i} {title}" in content


def test_serialize_conversation_deterministic():
    msgs = [user("u1"), assistant("a1")]
    assert serialize_conversation(msgs) == serialize_conversation(msgs)


def test_extract_summary_ok():
    assert extract_summary("abc<summary>hello</summary>yy") == "hello"


def test_extract_summary_fallback():
    assert extract_summary("no tags here") == "no tags here"


# ── 恢复三段 ───────────────────────────────────────────────────────


def _records(n: int, paths: list[str]) -> list[FileReadRecord]:
    out = []
    for i, p in enumerate(paths):
        out.append(
            FileReadRecord(
                path=p,
                content=f"content-{i}",
                timestamp=datetime(2026, 1, 1, 0, 0, i),  # noqa: DTZ001
            )
        )
    return out[:n]


def test_recovery_attachment_has_three_titles():
    text = build_recovery_attachment([], [])
    assert "## 最近读过的文件" in text
    assert "## 当前可用工具" in text
    assert "## 边界提示" in text


def test_recovery_attachment_limits_to_5_files():
    paths = [f"/tmp/f{i}.py" for i in range(7)]
    snapshot = _records(7, paths)[::-1]  # 时间戳倒序：f6 最新
    text = build_recovery_attachment(snapshot, [])
    for shown in ["/tmp/f6.py", "/tmp/f5.py", "/tmp/f4.py", "/tmp/f3.py", "/tmp/f2.py"]:
        assert shown in text
    for hidden in ["/tmp/f1.py", "/tmp/f0.py"]:
        assert hidden not in text


def test_recovery_attachment_truncates_long_file():
    long_content = "x" * (5000 * 4 + 100)  # 远超单文件 token 上限
    snapshot = [FileReadRecord(path="/tmp/big.py", content=long_content)]
    text = build_recovery_attachment(snapshot, [])
    assert "(content truncated)" in text


def test_recovery_tools_match():
    defs = [
        {"name": "read_file", "description": "Read", "input_schema": {"t": "o"}},
        {"name": "bash", "description": "Shell", "input_schema": {"t": "o"}},
    ]
    text = build_recovery_attachment([], defs)
    shown = set(re.findall(r"^- (\w+):", text, re.MULTILINE))
    assert shown == {"read_file", "bash"}


def test_recovery_attachment_deterministic():
    defs = [{"name": "read_file", "description": "R", "input_schema": {}}]
    snap = _records(2, ["/tmp/a.py", "/tmp/b.py"])
    a = build_recovery_attachment(snap, defs)
    b = build_recovery_attachment(snap, defs)
    assert a == b


# ── Layer2 纯函数 ──────────────────────────────────────────────────


def test_pick_recent_tail_returns_all_when_small():
    msgs = [user("hi"), assistant("yo")]
    tail = pick_recent_tail(msgs)
    assert [m.content for m in tail] == ["hi", "yo"]


def test_pick_recent_tail_stops_at_both_bounds():
    msgs = [user("x" * 40000) for _ in range(6)]
    tail = pick_recent_tail(msgs)
    assert len(tail) == RECENT_KEEP_MESSAGES
    assert tail[0] is msgs[1]  # 仅丢弃最旧一条


def test_pick_recent_tail_pair_fix():
    # 截断点落在 tool_result → 前推到配对 tool_use 之前
    msgs = [
        user("u0"),
        Message(
            role=MessageRole.ASSISTANT,
            content=[{"type": "tool_use", "id": "A", "name": "grep", "input": {}}],
            status=MessageStatus.COMPLETED,
        ),
        user_tool_result("A", "x" * 40000),
        user("u3"),
        assistant("a4"),
        user("u5"),
        assistant("a6"),
    ]
    tail = pick_recent_tail(msgs)
    # 修正后首条应是带 tool_use 的 assistant（索引 1）
    assert tail[0].role.value == "assistant"
    assert any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in tail[0].content
    )


def test_join_after_summary_inserts_assistant_placeholder():
    summary = user("SUMMARY")
    recent = [user("recent-user"), assistant("recent-ai")]
    out = _join_after_summary(summary, recent)
    assert out[0] is summary
    assert out[1].role.value == "assistant"
    assert len(out) == 4  # summary + 占位 assistant + recent 两条


def test_group_by_user_turn():
    msgs = [
        user("u1"),
        assistant("a1"),
        user_tool_result("t1", "r1"),
        user("u2"),
        assistant("a2"),
    ]
    groups = group_by_user_turn(msgs)
    assert len(groups) == 2
    assert len(groups[0]) == 3
    assert len(groups[1]) == 2


async def test_summarize_once_sends_no_tools(monkeypatch):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    text = await layer2.summarize_once(PROVIDER, "mock", [user("hi")])
    assert text == "RES"
    assert fake.tools_calls[0] is None  # tools=None


async def test_ptl_retry_drops_groups_until_success(monkeypatch):
    fake = FakeStreamingClient(["ptl", "ptl", "ptl", "ok:<summary>OK</summary>"])
    patch_create(monkeypatch, fake)
    # 5 组：前 3 次各丢 1 组（组数 5→4→3→2），第 4 次按比例丢 1 组（→1）后成功
    msgs = [user(f"m{i}") for i in range(5)]
    text = await layer2.ptl_retry(PROVIDER, "mock", msgs, PromptTooLongError("x"))
    assert text == "OK"
    assert fake.call_count == 4


async def test_ptl_retry_all_gone_raises(monkeypatch):
    fake = FakeStreamingClient(["ptl", "ptl", "ptl", "ptl", "ptl"])
    patch_create(monkeypatch, fake)
    msgs = [user("m0"), user("m1"), user("m2")]
    with pytest.raises(PromptTooLongError):
        await layer2.ptl_retry(PROVIDER, "mock", msgs, PromptTooLongError("first"))


async def test_run_summary_first_message_is_user(monkeypatch):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    new_msgs = await layer2.run_summary(
        PROVIDER, "mock", [], [], [user("hi"), assistant("yo")]
    )
    assert new_msgs[0].role.value == "user"
    assert "RES" in new_msgs[0].content


async def test_auto_compact_success_resets_breaker(monkeypatch):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    cb = CompactCircuitBreaker()
    cb.record_failure()
    await layer2.auto_compact(PROVIDER, "mock", [], [], [user("hi")], cb, 1000)
    assert cb._consecutive_failures == 0


async def test_auto_compact_failure_increments_breaker(monkeypatch):
    fake = FakeStreamingClient(["err"])
    patch_create(monkeypatch, fake)
    cb = CompactCircuitBreaker()
    with pytest.raises(RuntimeError):
        await layer2.auto_compact(PROVIDER, "mock", [], [], [user("hi")], cb, 1000)
    assert cb._consecutive_failures == 1


async def test_force_compact_failure_does_not_touch_breaker(monkeypatch):
    fake = FakeStreamingClient(["err"])
    patch_create(monkeypatch, fake)
    cb = CompactCircuitBreaker()
    with pytest.raises(RuntimeError):
        await layer2.force_compact(PROVIDER, "mock", [], [], [user("hi")], 1000)
    assert cb._consecutive_failures == 0


# ── manage_context 编排 ────────────────────────────────────────────


def _manage_input(
    conv: ConversationManager,
    session: SessionContext,
    *,
    context_window: int = 200000,
    estimated_token: int = 1000,
    usage_anchor: int = 0,
    anchor_msg_len: int = 0,
    trigger: TriggerKind = TriggerKind.AUTO,
    breaker: CompactCircuitBreaker | None = None,
) -> ManageInput:
    return ManageInput(
        conv=conv,
        provider_config=PROVIDER,
        model="mock",
        context_window=context_window,
        tool_defs=[],
        replacement=ContentReplacementState(),
        recovery=RecoveryState(),
        auto_tracking=breaker or CompactCircuitBreaker(),
        session=session,
        usage_anchor=usage_anchor,
        anchor_msg_len=anchor_msg_len,
        estimated_token=estimated_token,
        trigger=trigger,
    )


async def test_manage_context_auto_below_threshold_no_layer2(monkeypatch, tmp_path):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    conv = ConversationManager()
    conv.replace_history([user("hello")])
    # 阈值 = 200000 - 33000 = 167000；usage_anchor=0 + 少量消息远低于
    await manage_context(_manage_input(conv, make_session(tmp_path)))
    assert fake.call_count == 0  # 未触发 layer2
    assert len(conv.messages) == 1  # 历史未被摘要替换
    assert conv.messages[0].content == "hello"


async def test_manage_context_auto_triggers_layer2(monkeypatch, tmp_path):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    conv = ConversationManager()
    conv.replace_history([user("hello")])
    out_ = await manage_context(
        _manage_input(
            conv,
            make_session(tmp_path),
            usage_anchor=180000,  # 估算远超阈值
        )
    )
    assert fake.call_count == 1
    assert conv.messages[0].role.value == "user"
    assert "RES" in conv.messages[0].content
    assert out_.after_tokens < out_.before_tokens


async def test_manage_context_auto_window_too_small_skips(
    monkeypatch, tmp_path, caplog
):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    conv = ConversationManager()
    conv.replace_history([user("hello")])
    with caplog.at_level(logging.WARNING):
        await manage_context(
            _manage_input(
                conv,
                make_session(tmp_path),
                context_window=30000,  # ≤ SUMMARY_RESERVE + AUTO_SAFETY_MARGIN
                usage_anchor=180000,
            )
        )
    assert fake.call_count == 0
    assert any("context_window" in r.message for r in caplog.records)


async def test_manage_context_auto_breaker_tripped_skips_layer2(monkeypatch, tmp_path):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    cb = CompactCircuitBreaker()
    for _ in range(3):
        cb.record_failure()
    conv = ConversationManager()
    conv.replace_history([user("hello")])
    await manage_context(
        _manage_input(
            conv,
            make_session(tmp_path),
            usage_anchor=180000,
            breaker=cb,
        )
    )
    assert fake.call_count == 0


async def test_manage_context_manual_bypasses_threshold(monkeypatch, tmp_path):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    conv = ConversationManager()
    conv.replace_history([user("hello")])
    await manage_context(
        _manage_input(
            conv,
            make_session(tmp_path),
            estimated_token=1000,  # 远低于阈值，但 MANUAL 必须执行
            trigger=TriggerKind.MANUAL,
        )
    )
    assert fake.call_count == 1
    assert conv.messages[0].role.value == "user"


async def test_manage_context_emergency_runs_layer1_then_force(monkeypatch, tmp_path):
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    ctx = make_session(tmp_path)
    big = "E" * (SINGLE_RESULT_LIMIT + 1)
    conv = ConversationManager()
    conv.replace_history([user_tool_result("tid1", big)])
    await manage_context(_manage_input(conv, ctx, trigger=TriggerKind.EMERGENCY))

    # 紧急 layer1 生效：大结果已落盘
    assert (Path(ctx.spill_dir) / "tid1").exists()
    # force_compact 已重建历史
    assert fake.call_count == 1
    assert "RES" in conv.messages[0].content


# ── ConversationManager.replace_history ────────────────────────────


def test_replace_history_deep_copy():
    conv = ConversationManager()
    msgs = [user("hi")]
    conv.replace_history(msgs)
    msgs[0].content = "changed"
    assert conv.messages[0].content == "hi"


def test_replace_history_none_and_empty():
    conv = ConversationManager()
    conv.replace_history(None)
    assert conv.messages == []
    conv.replace_history([])
    assert conv.messages == []


# ── Config 协议默认窗口 ────────────────────────────────────────────


def test_effective_context_window_defaults():
    assert effective_context_window("anthropic", 0) == 200000
    assert effective_context_window("openai", 0) == 128000
    assert effective_context_window("anthropic", 80000) == 80000
    assert effective_context_window("unknown", 0) == 200000


# ── PromptTooLongError 哨兵 ────────────────────────────────────────


def test_prompt_too_long_error_is_exception():
    assert issubclass(PromptTooLongError, Exception)


# ── 空 replace 守卫（JSONL 重复回归）───────────────────────────────


async def test_manage_context_auto_noop_does_not_replace(monkeypatch, tmp_path):
    """layer1 无变化时跳过 replace_history，避免存档回调重复写历史。"""
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    replaced = []
    conv = ConversationManager(on_replace=replaced.append)
    conv.replace_history([user("hello")])
    replaced.clear()

    await manage_context(_manage_input(conv, make_session(tmp_path)))
    assert replaced == []  # 无落盘变化，不触发 on_replace
    assert fake.call_count == 0


async def test_manage_context_auto_spill_fires_replace(monkeypatch, tmp_path):
    """layer1 实际落盘时触发 on_replace（历史被改写）。"""
    fake = FakeStreamingClient(["ok:<summary>RES</summary>"])
    patch_create(monkeypatch, fake)
    replaced = []
    conv = ConversationManager(on_replace=replaced.append)
    big = "A" * (SINGLE_RESULT_LIMIT + 1)
    conv.replace_history([user_tool_result("tid1", big)])
    replaced.clear()
    ctx = make_session(tmp_path)

    await manage_context(_manage_input(conv, ctx))
    assert len(replaced) == 1  # 落盘改写历史 → 触发一次 on_replace
    assert (Path(ctx.spill_dir) / "tid1").exists()
