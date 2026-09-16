"""后端命令构造与 detect 单元测试。

tmux / iterm2 无法在 CI 实跑，断言其构造的 tmux/it2 命令参数正确（monkeypatch
create_subprocess_exec 收集 args）。in-process 后端测试见 test_team_manager 的 delete。
"""

from __future__ import annotations

import asyncio

import pytest

from core.team.backend import SpawnRequest, new_backend
from core.team.backend.detect import detect_backend
from core.team.backend.tmux import _build_member_cmd
from core.team.types import BackendType


def _make_subprocess_recorder(monkeypatch):
    """拦截 create_subprocess_exec，记录调用 args 并回一个假 proc。"""
    calls: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"pane-1\n", b""

        async def wait(self):
            return 0

    async def fake_create(*args, **kwargs):
        calls.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return calls


def _req():
    return SpawnRequest(
        team_name="demo",
        member_name="alice",
        agent_id="agent-a1b2c3",
        worktree_path="/abs/.mewcode/worktrees/team-demo+alice",
        session_dir="/abs/.mewcode/sessions/s1",
        agent_type="general-purpose",
        model="",
        initial_prompt="do the work",
        plan_mode_required=False,
    )


async def test_build_member_cmd_contains_agent_id():
    cmd = _build_member_cmd(_req())
    joined = " ".join(cmd)
    assert "--team-member" in cmd
    assert "--agent-id" in cmd
    assert "agent-a1b2c3" in joined
    assert "--team" in cmd and "demo" in joined
    assert "--member" in cmd and "alice" in joined


async def test_tmux_spawn_inside_tmux_uses_split_window(monkeypatch):
    calls = _make_subprocess_recorder(monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux.sock")
    backend = new_backend(BackendType.TMUX)
    _pane_id, agent_id = await backend.spawn(_req())
    assert agent_id == "agent-a1b2c3"
    # args[0]==tmux, [1]==split-window
    assert calls[0][0] == "tmux"
    assert "split-window" in calls[0]


async def test_tmux_spawn_outside_session_uses_new_session(monkeypatch):
    calls = _make_subprocess_recorder(monkeypatch)
    monkeypatch.delenv("TMUX", raising=False)
    backend = new_backend(BackendType.TMUX)
    await backend.spawn(_req())
    assert "new-session" in calls[0]


async def test_tmux_wake_send_keys(monkeypatch):
    calls = _make_subprocess_recorder(monkeypatch)
    backend = new_backend(BackendType.TMUX)
    await backend.wake("%5", "agent-x")
    assert calls[0][0] == "tmux"
    assert "send-keys" in calls[0]


async def test_iterm2_spawn_command(monkeypatch):
    calls = _make_subprocess_recorder(monkeypatch)
    backend = new_backend(BackendType.ITERM2)
    await backend.spawn(_req())
    assert calls[0][0] == "it2"
    assert "split" in calls[0]


async def test_iterm2_wake_send_text(monkeypatch):
    calls = _make_subprocess_recorder(monkeypatch)
    backend = new_backend(BackendType.ITERM2)
    await backend.wake("%1", "agent-x")
    assert calls[0][0] == "it2"
    assert "send-text" in calls[0]


def test_detect_tmux_env(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/sock")
    assert detect_backend() is BackendType.TMUX


def test_detect_iterm2(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setattr("core.team.backend.detect.shutil.which", lambda _: "/x/it2")
    assert detect_backend() is BackendType.ITERM2


def test_detect_tmux_binary(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)

    def _which(cmd):
        return "/usr/bin/tmux" if cmd == "tmux" else None

    monkeypatch.setattr("core.team.backend.detect.shutil.which", _which)
    assert detect_backend() is BackendType.TMUX


def test_detect_inprocess(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr("core.team.backend.detect.shutil.which", lambda _: None)
    assert detect_backend() is BackendType.IN_PROCESS


def test_new_backend_unknown():
    with pytest.raises(ValueError):
        new_backend("nope")  # type: ignore[arg-type]
