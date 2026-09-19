"""`codeforge attach` 的恢复提示（`main._pending_confirmations` / `_input_hint`）。

为什么单独测这一段：它的价值全在**文案是否可行动**。journal 里存的 `summary` 是
工具返回内容，写类工具成功时常常为空（实测 `write_file` 就返回空串），只报它会
打印出「- write_file: 」这种不含信息的告警。**调用参数**（写了哪个文件）才是用户
此刻要的，所以下面重点钉住参数一定能被带出来。

后半段（`_continue_run`）钉另一条更容易写错的性质：**续跑不碰被中断的会话**。
首期是「单活跃 run」，`attach --continue` 落在**当前** run 上，被 attach 的那个
历史 run 根本不会被加载——所以它必须一个字都不变。早先的 E2E 正是在这里写了
同义反复的断言（拿另一个会话的 journal 去比"有没有变"，永远相等），这条单测
是它的可入版本控制版本。
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from conversation.message import Message, MessageRole, MessageStatus
from core.archive.writer import Writer
from core.host import RunStatus
from core.host.journal import SideEffectJournal
from main import _continue_run, _input_hint, _pending_confirmations


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


# ── `_continue_run`：跨 run 必须明说，且不碰被中断的会话 ────────────────


class _FakeClient:
    """够 `_continue_run` 用的最小客户端：记下发了什么、事件序列是什么。"""

    def __init__(self, run_id: str, events: list[dict] | None = None) -> None:
        self.run_id = run_id
        self._events = (
            events if events is not None else [{"event": "AgentFinished", "data": {}}]
        )
        self.sent: list[str] = []
        self.closed = False

    async def get_run(self) -> dict:
        return {"session_id": "sess-live"}

    async def send_message(self, prompt: str) -> None:
        self.sent.append(prompt)

    async def events(self):
        for ev in self._events:
            yield ev

    def close(self) -> None:
        self.closed = True


def _session(tmp_path, session_id: str, *, with_pending: bool = False):
    """建一个会话目录；`with_pending=True` 造出 B 类（已执行但结果未落盘）。"""
    sdir = tmp_path / ".codeforge" / "sessions" / session_id
    sdir.mkdir(parents=True)
    msgs = [_tool_use("t1", tool_input={"file_path": "notes/x.txt"})]
    if not with_pending:
        # 补上配对结果 —— C 类，不该报警
        msgs.append(
            Message(
                role=MessageRole.USER,
                content="ok",
                status=MessageStatus.COMPLETED,
                tool_use_id="t1",
            )
        )
    _conversation(sdir, msgs)
    if with_pending:
        _journal(sdir, "t1")
    return sdir


def _snapshot(sdir):
    return {p.name: p.read_bytes() for p in sdir.iterdir() if p.is_file()}


async def test_continue_on_non_live_run_warns_and_leaves_session_untouched(
    tmp_path, capsys
):
    """被 attach 的是历史 run 时：明说续跑落在活跃 run 上，且那个会话一字未动。

    这条是 E2E 里那处同义反复的**可入版本控制版本**。原断言拿的是另一个会话的
    journal 去比"有没有变"——永远相等、不可能失败。这里改成：目标会话带着真实的
    B 类故障态，续跑之后它的**每个文件字节相同**，同时必须能看到跨 run 的告警。
    """
    sdir = _session(tmp_path, "sess-old", with_pending=True)
    before = _snapshot(sdir)

    record = SimpleNamespace(
        id="run-old", session_id="sess-old", status=RunStatus.INTERRUPTED
    )
    client = _FakeClient(run_id="run-live")

    rc = await _continue_run(client, record, argparse.Namespace(prompt="继续"), tmp_path)

    out = capsys.readouterr().out
    assert rc == 0
    # 恢复判定接进了产线，并且报的是**参数**（journal 的 summary 对 write_file 是空串）
    assert "可能已经生效" in out
    assert "tool_use_id = t1" in out
    assert "notes/x.txt" in out
    # 跨 run 必须说清楚，不能默默在别的会话上执行
    assert "不是当前活跃 run" in out
    # 续跑确实投出去了（否则上面的"没变"只是命令没干活）
    assert client.sent == ["继续"]
    assert client.closed is True
    # 硬要求：被 attach 的历史会话一个字节都没变
    assert _snapshot(sdir) == before


async def test_continue_on_live_run_has_no_cross_run_warning(tmp_path, capsys):
    """目标就是活跃 run 时不该出现跨 run 告警（否则告警会变成噪音）。"""
    sdir = _session(tmp_path, "sess-live")
    before = _snapshot(sdir)

    record = SimpleNamespace(
        id="run-live", session_id="sess-live", status=RunStatus.RUNNING
    )
    client = _FakeClient(run_id="run-live")

    rc = await _continue_run(client, record, argparse.Namespace(prompt=""), tmp_path)

    out = capsys.readouterr().out
    assert rc == 0
    assert "不是当前活跃 run" not in out
    # 干净会话（C 类）不该报假警报 —— 假警报会让人很快学会忽略真警报
    assert "没有「已执行但结果未落盘」的调用" in out
    # 省略 --prompt 时发默认的「继续」
    assert client.sent == ["继续"]
    assert _snapshot(sdir) == before


async def test_continue_returns_nonzero_on_agent_error(tmp_path, capsys):
    """回合出错要反映到退出码，不能一律返回 0。"""
    _session(tmp_path, "sess-live")
    record = SimpleNamespace(
        id="run-live", session_id="sess-live", status=RunStatus.RUNNING
    )
    client = _FakeClient(
        run_id="run-live",
        events=[{"event": "AgentError", "data": {"message": "上游 502"}}],
    )

    rc = await _continue_run(client, record, argparse.Namespace(prompt="继续"), tmp_path)

    assert rc == 1
    assert "上游 502" in capsys.readouterr().out
