"""Manager 队员 idle 通知单测（T30）。

handle_task_done：task 完成后把成员标 inactive，并向 Lead mailbox 写 idle 消息。
"""

from __future__ import annotations

from core.team.mailbox import Box
from core.team.manager import Manager
from core.team.types import BackendType, TeammateInfo


async def test_handle_task_done_marks_inactive_and_notifies(tmp_path, monkeypatch):
    def _detect():
        return BackendType.IN_PROCESS

    monkeypatch.setattr("core.team.manager.detect_backend", _detect)
    mgr = Manager(home_dir=tmp_path, wt_mgr=None, task_mgr=None, reg=None)
    team = await mgr.create("demo", "")
    await mgr.add_member(team, TeammateInfo(name="alice", agent_id="agent-a",
                                            backend_type=BackendType.IN_PROCESS))

    await mgr.handle_task_done("agent-a")

    # 成员标 inactive + 持久化
    assert team.member_by_name("alice").is_active is False
    from core.team.persistence import read_json

    raw = read_json(team.config_path)
    alice = next(m for m in raw["members"] if m["name"] == "alice")
    assert alice["is_active"] is False

    # Lead mailbox 收到 idle 消息
    box = Box(team.mailbox_dir)
    msgs = await box.read("lead")
    assert len(msgs) == 1
    assert "idle" in msgs[0].summary
    assert msgs[0].from_ == "alice"


async def test_handle_task_done_non_member_noop(tmp_path, monkeypatch):
    def _detect():
        return BackendType.IN_PROCESS

    monkeypatch.setattr("core.team.manager.detect_backend", _detect)
    mgr = Manager(home_dir=tmp_path, wt_mgr=None, task_mgr=None, reg=None)
    await mgr.create("demo", "")
    # agent-unknown 不是任何成员 → no-op 不抛
    await mgr.handle_task_done("agent-unknown")
