"""恢复语义与副作用 journal 的交叉判定测试。

这是整个 host 特性里最要紧的一条断言链：**崩溃后不能重复执行副作用**。
分两层验证：

1. `restore_session` 必须把「发起但没拿到结果」的工具调用透出来（而不是静默截掉），
   并且截断后剩下的对话必须是合法的（不能以「有 tool_call 无结果」或
   「有结果无 tool_call」结尾——两种都会被模型 API 判为非法请求）。
2. `find_pending_confirmations` 只在 journal 与未配对项**同时**命中时才报警：
   未配对但 journal 里没有 → 工具根本没跑，重跑安全，不该打扰用户。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from config.model import ProviderConfig
from conversation.message import Message, MessageRole, MessageStatus
from core.archive import read_unpaired_tool_uses
from core.archive.reader import restore_session
from core.archive.writer import Writer
from core.host.journal import SideEffectJournal, read_journal
from core.host.recovery import find_pending_confirmations

CONTEXT_WINDOW = 200_000


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        protocol="openai",
        model="gpt-4o",
        api_key="sk-test-not-a-real-key",
    )


def _tool_use(tid: str, name: str = "write_file") -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content="",
        status=MessageStatus.COMPLETED,
        tool_use_id=tid,
        tool_name=name,
        tool_input={"file_path": "a.txt"},
    )


def _tool_result(tid: str, content: str = "ok") -> Message:
    return Message(
        role=MessageRole.USER,
        content=content,
        status=MessageStatus.COMPLETED,
        tool_use_id=tid,
    )


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text, status=MessageStatus.COMPLETED)


def _write_conversation(session_dir, msgs: list[Message]) -> None:
    w = Writer(session_dir, model="gpt-4o")
    try:
        for m in msgs:
            w.append(m)
    finally:
        w.close()


async def _restore(session_dir):
    return await restore_session(
        session_dir, provider_config=_provider(), context_window=CONTEXT_WINDOW
    )


# ── 恢复：未配对项必须透出 ──────────────────────────────────────


async def test_unpaired_tool_use_is_reported(tmp_path):
    """末尾未配对调用必须出现在 RestoreResult 里，不再静默丢弃。"""
    _write_conversation(tmp_path, [_user("hi"), _tool_use("t1")])
    result = await _restore(tmp_path)
    assert [m.tool_use_id for m in result.unpaired_tool_uses] == ["t1"]
    assert result.unpaired_tool_uses[0].tool_name == "write_file"


async def test_complete_conversation_has_no_unpaired(tmp_path):
    """正常收尾的会话不该报未配对——否则恢复会一直打扰用户。"""
    _write_conversation(tmp_path, [_user("hi"), _tool_use("t1"), _tool_result("t1")])
    result = await _restore(tmp_path)
    assert result.unpaired_tool_uses == []
    assert len(result.conversation.messages) == 3


async def test_truncation_drops_whole_batch(tmp_path):
    """批次中途崩溃：[A(t1), A(t2), U(r1)] 必须整批截掉。

    只截到未配对的 A(t2) 会留下没有结果的 A(t1)，恢复后第一轮请求就会被
    模型 API 判为非法。
    """
    _write_conversation(
        tmp_path, [_tool_use("t1"), _tool_use("t2"), _tool_result("t1")]
    )
    result = await _restore(tmp_path)
    assert result.conversation.messages == []
    assert [m.tool_use_id for m in result.unpaired_tool_uses] == ["t2"]


async def test_truncation_keeps_completed_batches(tmp_path):
    """截断只吃未完成的批次，前面已完成的批次必须留下。"""
    _write_conversation(
        tmp_path,
        [
            _user("hi"),
            _tool_use("t0"),
            _tool_result("t0"),
            _tool_use("t1"),
            _tool_use("t2"),
            _tool_result("t1"),
        ],
    )
    result = await _restore(tmp_path)
    kept = [m.tool_use_id for m in result.conversation.messages]
    assert kept == [None, "t0", "t0"]
    assert [m.tool_use_id for m in result.unpaired_tool_uses] == ["t2"]


async def test_truncated_history_is_api_valid(tmp_path):
    """截断后的历史必须自洽：每个 tool_use 有结果、每个结果有请求。"""
    _write_conversation(
        tmp_path,
        [
            _user("hi"),
            _tool_use("t0"),
            _tool_result("t0"),
            _tool_use("t1"),
            _tool_use("t2"),
            _tool_result("t1"),
        ],
    )
    result = await _restore(tmp_path)
    msgs = result.conversation.messages
    use_ids = [m.tool_use_id for m in msgs if m.tool_name and m.tool_use_id]
    result_ids = [m.tool_use_id for m in msgs if m.tool_use_id and not m.tool_name]
    assert sorted(use_ids) == sorted(result_ids)


# ── journal 读取 ────────────────────────────────────────────────


def test_read_journal_missing_file(tmp_path):
    assert read_journal(tmp_path) == []


def test_read_journal_round_trip(tmp_path):
    j = SideEffectJournal(tmp_path)
    try:
        j.record(
            tool_use_id="t1", tool_name="write_file", category="write", result="ok"
        )
    finally:
        j.close()
    entries = read_journal(tmp_path)
    assert len(entries) == 1
    assert entries[0].tool_use_id == "t1"
    assert entries[0].tool_name == "write_file"
    assert entries[0].category == "write"
    assert entries[0].summary == "ok"


def test_read_journal_skips_half_written_line(tmp_path):
    """崩溃可能留下半行 JSON——半行不能变成异常，否则恢复本身会失败。"""
    path = tmp_path / "journal.jsonl"
    good = json.dumps(
        {
            "tool_use_id": "t1",
            "tool_name": "bash",
            "category": "command",
            "summary": "s",
            "ts": 1,
        }
    )
    path.write_text(good + "\n" + '{"tool_use_id": "t2", "tool', encoding="utf-8")
    entries = read_journal(tmp_path)
    assert [e.tool_use_id for e in entries] == ["t1"]


# ── 交叉判定 ────────────────────────────────────────────────────


def _journal_with(tmp_path, ids: list[str]) -> None:
    j = SideEffectJournal(tmp_path)
    try:
        for tid in ids:
            j.record(
                tool_use_id=tid,
                tool_name="write_file",
                category="write",
                result=f"wrote {tid}",
            )
    finally:
        j.close()


def test_pending_requires_journal_hit(tmp_path):
    """journal 里没有 → 工具根本没跑，重跑安全，不该报警。"""
    assert find_pending_confirmations(tmp_path, [_tool_use("t1")]) == []


def test_pending_reports_journal_hit(tmp_path):
    """journal 里有 + 对话里未配对 → 可能已生效，必须交人确认。"""
    _journal_with(tmp_path, ["t1"])
    pending = find_pending_confirmations(tmp_path, [_tool_use("t1")])
    assert [p.tool_use_id for p in pending] == ["t1"]
    assert pending[0].tool_name == "write_file"
    assert pending[0].summary == "wrote t1"


def test_pending_ignores_journal_entries_not_unpaired(tmp_path):
    """journal 里有但对话里已配对 → 结果已落盘，无事发生。"""
    _journal_with(tmp_path, ["t0", "t1"])
    pending = find_pending_confirmations(tmp_path, [_tool_use("t1")])
    assert [p.tool_use_id for p in pending] == ["t1"]


def test_pending_empty_when_nothing_unpaired(tmp_path):
    """没有未配对项时连 journal 都不用看。"""
    _journal_with(tmp_path, ["t1"])
    assert find_pending_confirmations(tmp_path, []) == []


async def test_end_to_end_interrupted_write_is_flagged(tmp_path):
    """端到端：写文件成功 → 结果未落盘 → 崩溃 → 恢复必须报出这一笔。"""
    j = SideEffectJournal(tmp_path)
    try:
        j.record(
            tool_use_id="t1",
            tool_name="write_file",
            category="write",
            result="File written: a.txt",
        )
    finally:
        j.close()
    # 对话里只有请求，没有结果（结果还没落盘进程就死了）
    _write_conversation(tmp_path, [_user("写个文件"), _tool_use("t1")])

    result = await _restore(tmp_path)
    pending = find_pending_confirmations(tmp_path, result.unpaired_tool_uses)
    assert [p.tool_use_id for p in pending] == ["t1"]


async def test_end_to_end_read_only_crash_is_safe(tmp_path):
    """端到端：崩在只读工具上 → journal 无记录 → 无需确认，重跑安全。"""
    _write_conversation(
        tmp_path, [_user("看看文件"), _tool_use("t1", name="read_file")]
    )
    result = await _restore(tmp_path)
    assert result.unpaired_tool_uses
    assert find_pending_confirmations(tmp_path, result.unpaired_tool_uses) == []


@pytest.mark.parametrize("tid", ["t1", "t2"])
def test_pending_preserves_journal_order(tmp_path, tid):
    _journal_with(tmp_path, ["t1", "t2"])
    pending = find_pending_confirmations(
        tmp_path, [_tool_use("t1"), _tool_use("t2")]
    )
    assert [p.tool_use_id for p in pending] == ["t1", "t2"]


# ── 只读取数（`read_unpaired_tool_uses`）──────────────────────────────
#
# `codeforge attach` 只看一眼状态，却会读**不是当前活跃 run** 的会话。
# 那条路径必须只读：`restore_session` 在超限压缩时会把 JSONL 整体重写
# （见其末尾的 `_persist_compact`），"看一眼就改写别人的会话文件"不可接受。
# 下面第一条断言就是这条契约本身。


def _conversation_bytes(session_dir) -> bytes:
    return (session_dir / "conversation.jsonl").read_bytes()


def test_read_unpaired_does_not_touch_the_file(tmp_path):
    """核心契约：读一次不得改写会话文件（逐字节比对，不是"内容等价"）。"""
    _write_conversation(tmp_path, [_user("hi"), _tool_use("t1")])
    before = _conversation_bytes(tmp_path)
    mtime_before = (tmp_path / "conversation.jsonl").stat().st_mtime_ns

    msgs = read_unpaired_tool_uses(tmp_path)

    assert _conversation_bytes(tmp_path) == before, "文件内容被改写了"
    assert (tmp_path / "conversation.jsonl").stat().st_mtime_ns == mtime_before
    assert [m.tool_use_id for m in msgs] == ["t1"]


def test_read_unpaired_matches_restore_session_result(tmp_path):
    """与 `restore_session` 的判定必须一致——两条路径分叉会让人对不上账。"""
    _write_conversation(
        tmp_path, [_user("hi"), _tool_use("t1"), _tool_use("t2"), _tool_result("t1")]
    )

    direct = read_unpaired_tool_uses(tmp_path)
    via_restore = asyncio.run(_restore(tmp_path)).unpaired_tool_uses

    assert [m.tool_use_id for m in direct] == [m.tool_use_id for m in via_restore]


def test_read_unpaired_on_complete_conversation_is_empty(tmp_path):
    _write_conversation(tmp_path, [_user("hi"), _tool_use("t1"), _tool_result("t1")])
    assert read_unpaired_tool_uses(tmp_path) == []


def test_read_unpaired_missing_file_returns_empty(tmp_path):
    """会话文件不存在 → 空列表，不抛。attach 的主职责是看状态，不该被判失败。"""
    assert read_unpaired_tool_uses(tmp_path / "nope") == []


def test_read_unpaired_chains_into_pending_confirmations(tmp_path):
    """两个来源交叉：未配对 × journal 同 id 命中 → 才报「可能已生效」。"""
    _write_conversation(tmp_path, [_user("hi"), _tool_use("t1")])
    _journal_with(tmp_path, ["t1"])

    pending = find_pending_confirmations(tmp_path, read_unpaired_tool_uses(tmp_path))

    assert [p.tool_use_id for p in pending] == ["t1"]
    assert pending[0].tool_name == "write_file"
