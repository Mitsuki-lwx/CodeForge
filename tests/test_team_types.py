"""团队基础类型单元测试。

覆盖：BackendType 枚举、Team / TeammateInfo 序列化 round-trip、
Team 查找 helper、异常类。
"""

from __future__ import annotations

from core.team.types import (
    BackendType,
    MemberNotFoundError,
    Team,
    TeamError,
    TeamHasActiveMembersError,
    TeammateInfo,
    TeamNotFoundError,
)


def test_backend_type_values():
    """BackendType 三个值齐全。"""
    assert {b.value for b in BackendType} == {"tmux", "iterm2", "in-process"}
    assert BackendType.IN_PROCESS == "in-process"


def test_teammate_round_trip():
    """TeammateInfo to_dict/from_dict round-trip 字段一致。"""
    info = TeammateInfo(
        name="alice",
        agent_id="agent-a1b2c3d",
        agent_type="worker",
        model="",
        worktree_path="/abs/.codeforge/worktrees/team-foo+alice",
        branch="worktree-team-foo+alice",
        backend_type=BackendType.TMUX,
        pane_id="%5",
        is_active=False,
        plan_mode_required=True,
        session_dir="/abs/.codeforge/sessions/abc",
    )
    d = info.to_dict()
    restored = TeammateInfo.from_dict(d)
    # is_active bool + enum + 字符串全部一致
    assert restored.name == info.name
    assert restored.agent_id == info.agent_id
    assert restored.backend_type is BackendType.TMUX
    assert restored.pane_id == "%5"
    assert restored.is_active is False
    assert restored.plan_mode_required is True
    assert restored.session_dir == info.session_dir


def test_teammate_is_active_none_round_trip():
    """is_active=None（活跃/存在但未标记空闲）必须保持 None，不被转成 False。"""
    info = TeammateInfo(name="lead", agent_id="lead")
    d = info.to_dict()
    assert d["is_active"] is None
    restored = TeammateInfo.from_dict(d)
    assert restored.is_active is None


def test_team_round_trip():
    """Team to_dict/from_dict round-trip，含成员与 created_at。"""
    team = Team(
        name="refactor auth",
        sanitized_name="refactor-auth",
        backend=BackendType.TMUX,
        members=[TeammateInfo(name="lead", agent_id="lead", is_active=None)],
    )
    d = team.to_dict()
    restored = Team.from_dict(d)
    assert restored.sanitized_name == "refactor-auth"
    assert restored.backend is BackendType.TMUX
    assert len(restored.members) == 1
    assert restored.members[0].name == "lead"
    # created_at 保留 isoformat
    assert restored.created_at == team.created_at


def test_team_derived_paths_not_persisted():
    """派生路径字段（config_dir 等）不写进 config，避免多余字段。"""
    team = Team(name="t", sanitized_name="t")
    team.config_dir = "/some/derived/path"
    d = team.to_dict()
    assert "config_dir" not in d
    assert "config_path" not in d


def test_member_lookup_helpers():
    team = Team(name="t", sanitized_name="t")
    team.members = [
        TeammateInfo(name="alice", agent_id="agent-1"),
        TeammateInfo(name="bob", agent_id="agent-2"),
    ]
    assert team.member_by_name("alice").agent_id == "agent-1"
    assert team.member_by_agent_id("agent-2").name == "bob"
    assert team.member_by_name("nobody") is None


def test_exceptions_share_base():
    """团队异常共享 TeamError 基类。"""
    assert issubclass(TeamNotFoundError, TeamError)
    assert issubclass(TeamHasActiveMembersError, TeamError)
    assert issubclass(MemberNotFoundError, TeamError)
