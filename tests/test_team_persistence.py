"""团队持久化单元测试。

覆盖：sanitize、atomic_write_json/read_json round-trip、reload_from_disk_locked。
"""

from __future__ import annotations

import pytest

from core.team.persistence import (
    atomic_write_json,
    read_json,
    reload_from_disk_locked,
    sanitize,
)
from core.team.types import Team, TeammateInfo


def test_sanitize_basic():
    assert sanitize("refactor auth") == "refactor-auth"
    assert sanitize("foo bar/baz") == "foo-bar-baz"
    assert sanitize("a.b._-c") == "a.b._-c"


def test_sanitize_edge():
    # 首尾补丁剪掉
    assert sanitize("-foo-") == "foo"
    assert sanitize("--") == ""
    assert sanitize("") == ""
    # 全特殊字符 → 全变 - 后整串被 strip 掉
    assert sanitize("!!! ###") == ""
    assert sanitize("!!!") == ""


def test_atomic_write_and_read(tmp_path):
    p = tmp_path / "config.json"
    atomic_write_json(p, {"a": 1, "list": [1, 2]})
    assert read_json(p) == {"a": 1, "list": [1, 2]}
    # 中间不留 .tmp
    assert not (tmp_path / "config.json.tmp").exists()


def test_atomic_write_round_trip_team(tmp_path):
    """Team 经 to_dict → atomic_write_json → read_json → from_dict 一致。"""
    team = Team(name="demo", sanitized_name="demo")
    team.members = [TeammateInfo(name="lead", agent_id="lead", is_active=None)]
    p = tmp_path / "config.json"
    atomic_write_json(p, team.to_dict())
    raw = read_json(p)
    restored = Team.from_dict(raw)
    assert restored.sanitized_name == "demo"
    assert restored.members[0].name == "lead"
    assert restored.members[0].is_active is None


def test_read_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_json(tmp_path / "nope.json")


async def test_reload_from_disk_locked_overwrites_members(tmp_path):
    """F19c：reload 后 disk members 覆盖内存（跨进程并发兜底的关键）。

    子进程内存 Team 里 alice 尚未标记空闲（is_active=None），但 disk 上另一进程
    已把它记成 is_active=False。reload_from_disk_locked 必须反映 disk，而非内存旧值。
    """
    p = tmp_path / "config.json"
    team = Team(name="demo", sanitized_name="demo")
    team.config_path = str(p)
    team.config_dir = str(tmp_path)
    # 子进程内存视角：lead + alice（is_active=None，尚未空闲）
    team.members = [
        TeammateInfo(name="lead", agent_id="lead", is_active=None),
        TeammateInfo(name="alice", agent_id="agent-a", is_active=None),
    ]
    # 另一进程写盘：把 alice 标成已空闲 is_active=False
    disk_view = Team.from_dict(team.to_dict())
    disk_view.member_by_name("alice").is_active = False
    atomic_write_json(p, disk_view.to_dict())

    # reload 覆盖内存 → alice 应反映 disk 最新，而非静默 no-op 保持 None
    await reload_from_disk_locked(team)
    alice = team.member_by_name("alice")
    assert alice is not None
    assert alice.is_active is False


async def test_reload_from_disk_locked_missing_config_is_noop(tmp_path):
    """config 缺失时不抛错，内存 members 保持原样。"""
    team = Team(name="demo", sanitized_name="demo")
    team.members.append(TeammateInfo(name="lead", agent_id="lead"))
    team.config_path = str(tmp_path / "missing.json")
    await reload_from_disk_locked(team)  # 不应抛错
    assert len(team.members) == 1
