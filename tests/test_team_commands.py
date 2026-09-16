"""/team 命令单元测试。

假 UI 实现 team_list/info/delete/kill，验证 handle_team 分发与输出。
"""

from __future__ import annotations

from core.commands.builtin_team import handle_team


class _FakeUI:
    def __init__(self, teams=None, info=None, delete_msg="", kill_msg="") -> None:
        self.teams = teams or []
        self.info = info
        self.delete_msg = delete_msg
        self.kill_msg = kill_msg
        self.lines: list[str] = []
        self.errors: list[str] = []
        self.deleted: list[tuple] = []
        self.killed: list[str] = []

    def println(self, msg: str) -> None:
        self.lines.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def team_list(self) -> list[dict]:
        return self.teams

    def team_info(self, name: str):
        return self.info

    async def team_delete(self, name: str, force: bool = False) -> str:
        self.deleted.append((name, force))
        return self.delete_msg

    async def team_kill(self, member: str) -> str:
        self.killed.append(member)
        return self.kill_msg


async def test_team_list_empty():
    ui = _FakeUI(teams=[])
    await handle_team(ui, "list")
    assert any("No teams" in l for l in ui.lines)


async def test_team_list_shows_summary():
    ui = _FakeUI(teams=[
        {"name": "demo", "backend": "in-process", "members": [
            {"is_active": None}, {"is_active": False},
        ]},
    ])
    await handle_team(ui, "list")
    joined = "\n".join(ui.lines)
    assert "demo" in joined
    assert "in-process" in joined
    assert "[1/2] 活跃" in joined


async def test_team_info():
    ui = _FakeUI(info={
        "name": "demo", "backend": "in-process", "config_path": "/x/config.json",
        "members": [
            {"name": "alice", "agent_id": "agent-a", "backend_type": "in-process",
             "is_active": None, "worktree_path": "/wt"},
        ],
    })
    await handle_team(ui, "info demo")
    joined = "\n".join(ui.lines)
    assert "alice" in joined
    assert "agent-a" in joined


async def test_team_delete_force():
    ui = _FakeUI(delete_msg="")
    await handle_team(ui, "delete demo --force")
    assert ui.deleted == [("demo", True)]


async def test_team_delete_no_force():
    ui = _FakeUI(delete_msg="有活跃成员")
    await handle_team(ui, "delete demo")
    assert ui.deleted == [("demo", False)]
    assert ui.errors == ["有活跃成员"]


async def test_team_kill():
    ui = _FakeUI(kill_msg="")
    await handle_team(ui, "kill alice")
    assert ui.killed == ["alice"]
    assert any("alice" in l and "终止" in l for l in ui.lines)


async def test_team_unknown_subcommand():
    ui = _FakeUI()
    await handle_team(ui, "bogus")
    assert any("Unknown subcommand" in e for e in ui.errors)
