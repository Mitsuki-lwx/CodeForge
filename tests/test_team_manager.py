"""团队 Manager 单元测试。

覆盖：目录自动创建、scan 还原、create（sanitize + 同名后缀 + 后端）、
delete（非 force 拦截/force 清理）、add_member / set_member_active / remove_member、
F19c reload 兜底。
"""

from __future__ import annotations

import pytest

from core.team.manager import Manager
from core.team.persistence import read_json
from core.team.types import (
    BackendType,
    MemberExistsError,
    MemberNotFoundError,
    TeamHasActiveMembersError,
    TeammateInfo,
    TeamNotFoundError,
)


class _FakeWtMgr:
    """假 worktree 管理器：记录 delete 调用。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, name: str):
        self.deleted.append(name)
        return True, ""


class _FakeTaskMgr:
    def __init__(self) -> None:
        self.killed: list[str] = []

    async def stop(self, agent_id: str):
        self.killed.append(agent_id)
        return True


def _make_mgr(tmp_path, monkeypatch):
    """构造指向 tmp 主目录的 Manager，后端检测固定为 in-process。"""
    monkeypatch.setattr(
        "core.team.manager.detect_backend",
        lambda: BackendType.IN_PROCESS,
    )
    return Manager(
        home_dir=tmp_path,
        wt_mgr=_FakeWtMgr(),
        task_mgr=_FakeTaskMgr(),
        reg=None,
    )


async def test_init_creates_teams_dir(tmp_path, monkeypatch):
    _make_mgr(tmp_path, monkeypatch)
    assert (tmp_path / ".codeforge" / "teams").is_dir()


async def test_create_sanitizes_and_writes_config(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    team = await mgr.create("refactor auth", "")
    assert team.sanitized_name == "refactor-auth"
    cfg = tmp_path / ".codeforge" / "teams" / "refactor-auth" / "config.json"
    assert cfg.exists()
    assert mgr.get("refactor-auth") is team


async def test_same_name_gets_suffix(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    t1 = await mgr.create("demo", "")
    t2 = await mgr.create("demo", "")
    assert t1.sanitized_name == "demo"
    assert t2.sanitized_name == "demo-2"
    assert mgr.get("demo") is t1
    assert mgr.get("demo-2") is t2


async def test_create_empty_sanitize_rejected(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    with pytest.raises(TeamNotFoundError):
        await mgr.create("!!!", "")


async def test_list_sorted_by_created(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    t1 = await mgr.create("first", "")
    t2 = await mgr.create("second", "")
    assert [t.sanitized_name for t in mgr.list_()] == ["first", "second"]
    assert t1.created_at <= t2.created_at


async def test_scan_recovers_existing_teams(tmp_path, monkeypatch):
    _make_mgr(tmp_path, monkeypatch)  # 建目录
    mgr2 = _make_mgr(tmp_path, monkeypatch)
    await mgr2.create("existing", "")
    # 新 Manager 从 disk 扫描还原
    mgr3 = Manager(home_dir=tmp_path, wt_mgr=None, task_mgr=None, reg=None)
    assert mgr3.get("existing") is not None


async def test_scan_skips_malformed(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".codeforge" / "teams" / "broken"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("{not valid json", encoding="utf-8")
    mgr = _make_mgr(tmp_path, monkeypatch)
    assert mgr.get("broken") is None  # 跳过不崩


async def test_delete_rejects_active_members(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    team = await mgr.create("demo", "")
    team.members.append(
        TeammateInfo(name="alice", agent_id="agent-a", is_active=True)
    )
    mgr._persist(team)
    with pytest.raises(TeamHasActiveMembersError):
        await mgr.delete("demo", force=False)
    assert mgr.get("demo") is not None  # 目录仍在


async def test_delete_force_cleans(tmp_path, monkeypatch):
    wt = _FakeWtMgr()
    task = _FakeTaskMgr()
    monkeypatch.setattr(
        "core.team.manager.detect_backend", lambda: BackendType.IN_PROCESS
    )
    mgr = Manager(home_dir=tmp_path, wt_mgr=wt, task_mgr=task, reg=None)
    team = await mgr.create("demo", "")
    team.members.append(
        TeammateInfo(
            name="bob",
            agent_id="agent-b",
            backend_type=BackendType.IN_PROCESS,
            session_dir=str(tmp_path / "sess-bob"),
        )
    )
    mgr._persist(team)
    (tmp_path / "sess-bob").mkdir()
    await mgr.delete("demo", force=True)
    assert mgr.get("demo") is None
    assert not (tmp_path / ".codeforge" / "teams" / "demo").exists()
    assert not (tmp_path / "sess-bob").exists()
    assert task.killed == ["agent-b"]


async def test_add_set_active_remove(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    team = await mgr.create("demo", "")
    await mgr.add_member(team, TeammateInfo(name="alice", agent_id="agent-a"))
    assert team.member_by_name("alice") is not None
    await mgr.set_member_active(team, "alice", False)
    assert team.member_by_name("alice").is_active is False
    await mgr.remove_member(team, "alice")
    assert team.member_by_name("alice") is None
    # disk 同步
    raw = read_json(team.config_path)
    names = [m["name"] for m in raw["members"]]
    assert "alice" not in names


async def test_member_add_duplicate_raises(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    team = await mgr.create("demo", "")
    await mgr.add_member(team, TeammateInfo(name="alice", agent_id="agent-a"))
    with pytest.raises(MemberExistsError):
        await mgr.add_member(team, TeammateInfo(name="alice", agent_id="agent-a"))


async def test_member_ops_not_found(tmp_path, monkeypatch):
    mgr = _make_mgr(tmp_path, monkeypatch)
    team = await mgr.create("demo", "")
    with pytest.raises(MemberNotFoundError):
        await mgr.set_member_active(team, "nobody", True)
    with pytest.raises(MemberNotFoundError):
        await mgr.remove_member(team, "nobody")


async def test_set_member_active_reload_f19c(tmp_path, monkeypatch):
    """F19c：另一进程在 disk 加了 alice 后，本进程 set_member_active 不丢更新。"""
    mgr = _make_mgr(tmp_path, monkeypatch)
    team = await mgr.create("demo", "")
    # 模拟另一进程把 alice 写入 disk config（此时本进程内存 team 没有 alice）
    disk_team = team.__class__.from_dict(team.to_dict())
    disk_team.members.append(
        TeammateInfo(name="alice", agent_id="agent-a", is_active=None)
    )
    from core.team.persistence import atomic_write_json

    atomic_write_json(team.config_path, disk_team.to_dict())
    # 本进程内存 team 尚未 reload，仍只有 lead。
    # set_member_active 触发 reload_from_disk_locked → 应能找到 alice 并改为 False。
    await mgr.set_member_active(team, "alice", False)
    assert team.member_by_name("alice").is_active is False
