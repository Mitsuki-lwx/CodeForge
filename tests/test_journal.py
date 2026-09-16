"""副作用 journal 测试。

覆盖两层：
1. `SideEffectJournal` 自身——字段、截断、追加语义、写失败降级（不抛、只告警）。
2. 与 Agent 的接线——`_exec_one` 只对 write/command 类成功调用记账，读操作不记，
   失败的调用不记。这层是「journal 有没有真的接上」的护栏：只测单元而不测接线，
   最容易出现「类写好了但没人调用」。
"""

from __future__ import annotations

import json

import pytest

from config.model import ProviderConfig
from core.agent.bootstrap import build_session
from core.host import journal as journal_mod
from core.host.journal import (
    JOURNAL_FILENAME,
    SIDE_EFFECT_CATEGORIES,
    SideEffectJournal,
)
from llm.stream_events import ToolUse


def _entries(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ── 单元：写入器 ────────────────────────────────────────────────


def test_record_appends_entry_with_expected_fields(tmp_path):
    j = SideEffectJournal(tmp_path)
    try:
        assert j.record(
            tool_use_id="t1", tool_name="write_file", category="write", result="ok"
        )
    finally:
        j.close()

    entries = _entries(tmp_path / JOURNAL_FILENAME)
    assert len(entries) == 1
    assert entries[0]["tool_use_id"] == "t1"
    assert entries[0]["tool_name"] == "write_file"
    assert entries[0]["category"] == "write"
    assert entries[0]["summary"] == "ok"
    assert isinstance(entries[0]["ts"], int)


def test_record_is_append_only(tmp_path):
    """journal 只追加：两次调用留两行，不去重不重写。"""
    j = SideEffectJournal(tmp_path)
    try:
        j.record(tool_use_id="t1", tool_name="bash", category="command", result="a")
        j.record(tool_use_id="t2", tool_name="bash", category="command", result="b")
    finally:
        j.close()
    assert [e["tool_use_id"] for e in _entries(tmp_path / JOURNAL_FILENAME)] == [
        "t1",
        "t2",
    ]


def test_summary_is_truncated(tmp_path):
    """journal 是线索不是结果备份，长结果必须截断。"""
    j = SideEffectJournal(tmp_path, summary_max_chars=10)
    try:
        j.record(
            tool_use_id="t1", tool_name="bash", category="command", result="x" * 500
        )
    finally:
        j.close()
    assert _entries(tmp_path / JOURNAL_FILENAME)[0]["summary"] == "x" * 10


def test_write_failure_degrades_without_raising(tmp_path):
    """写失败必须降级（返回 False），绝不把工具执行打断。"""
    j = SideEffectJournal(tmp_path)
    j.close()  # 句柄已关，后续写入必然失败
    assert (
        j.record(tool_use_id="t1", tool_name="bash", category="command", result="a")
        is False
    )


def test_write_failure_warns_only_once(tmp_path, caplog):
    """降级后只告警一次——逐条刷日志会把真正的错误淹掉。"""
    j = SideEffectJournal(tmp_path)
    j.close()
    with caplog.at_level("WARNING", logger=journal_mod.__name__):
        for i in range(3):
            j.record(
                tool_use_id=f"t{i}", tool_name="bash", category="command", result="a"
            )
    assert len([r for r in caplog.records if "journal" in r.message]) == 1


def test_journal_file_is_in_session_dir(tmp_path):
    assert journal_mod.journal_path(tmp_path) == tmp_path / JOURNAL_FILENAME


def test_side_effect_categories_are_write_and_command():
    """只有会改动工作区的类别需要记账；读操作记了只会让文件膨胀。"""
    assert SIDE_EFFECT_CATEGORIES == {"write", "command"}


# ── 接线：Agent._exec_one ───────────────────────────────────────


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        protocol="openai",
        model="gpt-4o",
        api_key="sk-test-not-a-real-key",
    )


async def test_agent_records_write_tool(tmp_path):
    """写文件成功后必须留下一条 write 记录。"""
    bundle = await build_session(provider=_provider(), workspace=tmp_path)
    try:
        target = tmp_path / "notes.txt"
        ok, _content, _ms, _meta = await bundle.agent._exec_one(
            ToolUse(
                id="t1",
                name="write_file",
                input={"file_path": str(target), "content": "hi"},
            )
        )
        assert ok
        entries = _entries(bundle.journal.path)
        assert [e["tool_use_id"] for e in entries] == ["t1"]
        assert entries[0]["category"] == "write"
    finally:
        bundle.close()


async def test_agent_does_not_record_read_tool(tmp_path):
    """读文件没有副作用，不该进 journal。"""
    bundle = await build_session(provider=_provider(), workspace=tmp_path)
    try:
        target = tmp_path / "notes.txt"
        target.write_text("hello", encoding="utf-8")
        ok, _content, _ms, _meta = await bundle.agent._exec_one(
            ToolUse(id="t1", name="read_file", input={"file_path": str(target)})
        )
        assert ok
        assert _entries(bundle.journal.path) == []
    finally:
        bundle.close()


async def test_agent_does_not_record_failed_tool(tmp_path):
    """失败的调用没有产生副作用，不能记——否则恢复时会白白多问一次。"""
    bundle = await build_session(provider=_provider(), workspace=tmp_path)
    try:
        ok, _content, _ms, _meta = await bundle.agent._exec_one(
            ToolUse(id="t1", name="read_file", input={"file_path": str(tmp_path / "nope")})
        )
        assert not ok
        assert _entries(bundle.journal.path) == []
    finally:
        bundle.close()


async def test_agent_records_command_tool(tmp_path):
    """命令执行也算副作用（可能改了工作区），必须记。"""
    bundle = await build_session(provider=_provider(), workspace=tmp_path)
    try:
        ok, _content, _ms, _meta = await bundle.agent._exec_one(
            ToolUse(id="t1", name="bash", input={"command": "echo hi"})
        )
        assert ok
        entries = _entries(bundle.journal.path)
        assert [e["category"] for e in entries] == ["command"]
    finally:
        bundle.close()


async def test_journal_disabled_by_default_is_noop(tmp_path):
    """不传 journal 时 Agent 完全不做记账（默认路径零开销）。"""
    from core.agent.agent import Agent

    bundle = await build_session(provider=_provider(), workspace=tmp_path)
    try:
        bare = Agent(
            registry=bundle.registry,
            llm_client=bundle.agent._client,
            exec_ctx=bundle.agent._exec_ctx,
            conversation=bundle.conversation,
        )
        assert bare._journal is None
        ok, _c, _ms, _meta = await bare._exec_one(
            ToolUse(
                id="t9",
                name="write_file",
                input={"file_path": str(tmp_path / "x.txt"), "content": "y"},
            )
        )
        assert ok
        assert _entries(bundle.journal.path) == []
    finally:
        bundle.close()


@pytest.mark.parametrize("category", ["write", "command"])
def test_record_accepts_both_side_effect_categories(tmp_path, category):
    j = SideEffectJournal(tmp_path)
    try:
        assert j.record(
            tool_use_id="t1", tool_name="x", category=category, result="r"
        )
    finally:
        j.close()
