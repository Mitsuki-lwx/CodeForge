"""`codeforge attach` 的恢复提示（`main._pending_confirmations` / `_input_hint`）。

为什么单独测这一段：它的价值全在**文案是否可行动**。journal 里存的 `summary` 是
工具返回内容，写类工具成功时常常为空（实测 `write_file` 就返回空串），只报它会
打印出「- write_file: 」这种不含信息的告警。**调用参数**（写了哪个文件）才是用户
此刻要的，所以下面重点钉住参数一定能被带出来。
"""

from __future__ import annotations

import pytest

from conversation.message import Message, MessageRole, MessageStatus
from core.archive.writer import Writer
from core.host.journal import SideEffectJournal
from main import _input_hint, _pending_confirmations


def _conversation(session_dir, msgs: list[Message]) -> None:
    w = Writer(session_dir, model="gpt-4o")
    try:
        for m in msgs:
            w.append(m)
    finally:
        w.close()


def _tool_use(
    tid: str, name: str = "write_file", tool_input: dict | None = None
) -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content="",
        status=MessageStatus.COMPLETED,
        tool_use_id=tid,
        tool_name=name,
        tool_input=tool_input if tool_input is not None else {"file_path": "a.txt"},
    )


def _journal(session_dir, tid: str, name: str = "write_file", result: str = "") -> None:
    j = SideEffectJournal(session_dir)
    try:
        j.record(tool_use_id=tid, tool_name=name, category="write", result=result)
    finally:
        j.close()


def test_pending_carries_tool_arguments(tmp_path):
    """核心：参数必须带出来 —— journal 的 summary 为空时它是唯一线索。"""
    _conversation(
        tmp_path, [_tool_use("t1", tool_input={"file_path": "notes/e2e.txt"})]
    )
    _journal(tmp_path, "t1")

    pending = _pending_confirmations(tmp_path)

    assert len(pending) == 1
    assert pending[0].tool_name == "write_file"
    assert pending[0].tool_use_id == "t1"
    assert _input_hint(pending[0].tool_input) == "notes/e2e.txt"


def test_pending_exposes_nonempty_summary_when_present(tmp_path):
    """journal 有返回内容时也要透出来（它是「上次跑到哪」的线索）。"""
    _conversation(tmp_path, [_tool_use("t1")])
    _journal(tmp_path, "t1", result="wrote 12 bytes")

    pending = _pending_confirmations(tmp_path)

    assert pending[0].summary == "wrote 12 bytes"


def test_pending_empty_when_journal_has_no_hit(tmp_path):
    """未配对但 journal 无记录 → 工具根本没跑，重跑安全，不该打扰用户。"""
    _conversation(tmp_path, [_tool_use("t1")])
    assert _pending_confirmations(tmp_path) == []


def test_pending_empty_on_missing_session(tmp_path):
    """会话不存在 → 空列表，不抛（attach 不该被恢复判定拖垮）。"""
    assert _pending_confirmations(tmp_path / "nope") == []


def test_pending_swallows_unexpected_errors(tmp_path, monkeypatch):
    """任何意外都不该让 attach 失败 —— 查看状态才是它的主职责。"""

    def _boom(_):
        raise RuntimeError("模拟恢复判定内部炸了")

    monkeypatch.setattr("core.archive.read_unpaired_tool_uses", _boom)
    assert _pending_confirmations(tmp_path) == []


@pytest.mark.parametrize(
    ("tool_input", "expected"),
    [
        ({"file_path": "a/b.txt"}, "a/b.txt"),
        ({"path": "c.txt"}, "c.txt"),
        ({"command": "ls -la"}, "ls -la"),
        ({"pattern": "*.py"}, "*.py"),
        ({"url": "http://x"}, "http://x"),
        # 优先级：file_path 先于 path
        ({"path": "b.txt", "file_path": "a.txt"}, "a.txt"),
        # 挑不到就空串 —— 调用方据此省略冒号，而不是打印一个空占位
        ({}, ""),
        ({"content": "只有内容，没有位置"}, ""),
        ({"file_path": ""}, ""),
        ({"file_path": None}, ""),
    ],
)
def test_input_hint_picks_the_actionable_field(tool_input, expected):
    assert _input_hint(tool_input) == expected
