"""spawn_teammate 编排单测。

对 `_build_teammate_agent/_build_teammate_conv` 做 monkeypatch（避免构造真实 Agent 需完整
LLM client），只验证 in-process 派生的编排：worktree 创建、add_member、名册注册、task_mgr.launch。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.team.manager import Manager
from core.team.registry import AgentNameRegistry
from core.team.spawn import build_team_context_reminder, spawn_teammate
from core.team.types import BackendType, TeammateInfo, TeamNotFoundError


class _FakeWT:
    def __init__(self, path):
        self.path = path


class _FakeWTMgr:
    def __init__(self, d) -> None:
        self.d = d
        self.calls: list[str] = []

    async def create(self, name, owner=""):
        self.calls.append(name)
        p = Path(self.d) / name.replace("/", "_")
        p.mkdir(parents=True, exist_ok=True)
        return _FakeWT(str(p))


class _FakeTaskMgr:
    def __init__(self) -> None:
        self.launched: list[tuple] = []

    async def launch(self, agent, conv, name="", task_text=""):
        self.launched.append((name, task_text))
        return "task_x1"


async def _make_mgr(tmp_path, monkeypatch):
    def _detect():
        return BackendType.IN_PROCESS

    monkeypatch.setattr("core.team.manager.detect_backend", _detect)
    return Manager(home_dir=tmp_path, wt_mgr=None, task_mgr=None, reg=None)


async def test_spawn_teammate_inprocess(tmp_path, monkeypatch):
    mgr = await _make_mgr(tmp_path, monkeypatch)
    team = await mgr.create("demo", "")
    wt = _FakeWTMgr(str(tmp_path))
    tm = _FakeTaskMgr()
    reg = AgentNameRegistry()

    # 不构造真实 Agent：patch 构建函数返回假对象
    class _FakeSubAgent:
        pass

    monkeypatch.setattr(
        "core.team.spawn._build_teammate_agent",
        lambda **kw: _FakeSubAgent(),
    )
    monkeypatch.setattr(
        "core.team.spawn._build_teammate_conv",
        lambda **kw: object(),
    )


    class _Registry:
        def list(self):
            return []

    class _FakeParent:
        def __init__(self):
            self._registry = _Registry()
            self._worktree_session = None
            self._client = None
            self._runtime = None
            self._hooks = None
            self._exec_ctx = type("X", (), {"cwd": Path(str(tmp_path))})()

    parent = _FakeParent()

    out = await spawn_teammate(
        manager=mgr, parent_agent=parent, worktree_mgr=wt,
        task_mgr=tm, name_reg=reg,
        team_name="demo", member_name="alice",
        prompt="echo hello > /tmp/x",
    )
    assert "alice" in out
    assert "in-process" in out
    # worktree 用 team-demo/alice
    assert "team-demo/alice" in wt.calls
    # 成员已加入并持久化
    assert team.member_by_name("alice") is not None
    from core.team.persistence import read_json

    raw = read_json(team.config_path)
    assert "alice" in [m["name"] for m in raw["members"]]
    # 名册注册 + 任务已 launch
    assert reg.resolve("alice") is not None
    assert tm.launched == [("alice", "echo hello > /tmp/x")]


async def test_spawn_unknown_team(tmp_path, monkeypatch):
    mgr = await _make_mgr(tmp_path, monkeypatch)
    wt = _FakeWTMgr(str(tmp_path))
    with pytest.raises(TeamNotFoundError):
        await spawn_teammate(
            manager=mgr, parent_agent=object(), worktree_mgr=wt,
            task_mgr=_FakeTaskMgr(), name_reg=None,
            team_name="nope", member_name="alice", prompt="x",
        )


def test_build_team_context_reminder():
    info = TeammateInfo(name="alice", agent_id="agent-a", agent_type="worker")
    team = type("T", (), {"name": "demo", "members": [info]})()

    text = build_team_context_reminder(team, info)
    assert "<team-context>" in text
    assert "team: demo" in text
    assert "你的成员名: alice" in text
    assert "agent-a" in text
