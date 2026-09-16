"""T31 Lead 邮箱 watcher / 轮询单测。

覆盖：Manager.poll_lead_mailboxes 读取+标 read；build_team_update_reminder 格式与截断；
begin_autonomous_turn 文本。
"""

from __future__ import annotations

from core.team.mailbox import Box
from core.team.mailbox.message import Message, MessageType
from core.team.manager import Manager
from core.team.types import BackendType, TeammateInfo
from tui.lead_mail import (
    TEAM_UPDATE_TRUNCATE,
    begin_autonomous_turn_text,
    build_team_update_reminder,
)


async def _mgr_with_lead_msg(tmp_path, monkeypatch):
    def _detect():
        return BackendType.IN_PROCESS

    monkeypatch.setattr("core.team.manager.detect_backend", _detect)
    mgr = Manager(home_dir=tmp_path, wt_mgr=None, task_mgr=None, reg=None)
    team = await mgr.create("demo", "")
    await mgr.add_member(team, TeammateInfo(name="alice", agent_id="agent-a"))
    # alice 给 Lead 发一条 idle 消息
    box = Box(team.mailbox_dir)
    await box.write(
        "lead",
        Message(from_="alice", to="lead", type=MessageType.TEXT,
                summary="alice idle", content="finished the work"),
    )
    return mgr


async def test_poll_lead_mailboxes_reads_and_marks_read(tmp_path, monkeypatch):
    mgr = await _mgr_with_lead_msg(tmp_path, monkeypatch)
    msgs = await mgr.poll_lead_mailboxes()
    assert len(msgs) == 1
    assert msgs[0]["from_"] == "alice"
    assert msgs[0]["summary"] == "alice idle"
    # 读后被标 read，再次 poll 为空
    assert await mgr.poll_lead_mailboxes() == []


def test_build_team_update_reminder_format():
    text = build_team_update_reminder([
        {"team_name": "demo", "from_": "alice", "type": "text",
         "summary": "alice idle", "content": "done"},
    ])
    assert "<team-update>" in text
    assert "team=demo" in text
    assert "from=alice" in text


def test_build_team_update_reminder_truncates_content():
    content = "x" * (TEAM_UPDATE_TRUNCATE + 100)
    text = build_team_update_reminder([
        {"team_name": "demo", "from_": "a", "type": "text",
         "summary": "s", "content": content},
    ])
    assert "…[截断]" in text
    assert len(text) < TEAM_UPDATE_TRUNCATE + 300


def test_build_team_update_reminder_empty():
    assert build_team_update_reminder([]) == ""


def test_begin_autonomous_turn_text():
    assert "team-update" in begin_autonomous_turn_text()
