"""BackgroundTaskManager 团队集成单测。

覆盖：set_name_registry 委托 + on_task_done 回调触发。
"""

from __future__ import annotations

import asyncio

import pytest

import core.agent.sub_agent as sub_agent_mod
from core.task.manager import BackgroundTaskManager
from core.team.registry import AgentNameRegistry


@pytest.fixture
def fake_run(monkeypatch):
    async def _fake(agent, conv, task="", events=None):
        return "done"

    monkeypatch.setattr(sub_agent_mod, "run_to_completion", _fake)


class _Agent:
    pass


async def test_on_task_done_fires(fake_run):
    mgr = BackgroundTaskManager()
    done: list[str] = []

    async def cb(task_id):
        done.append(task_id)

    mgr.on_task_done(cb)
    task_id = await mgr.launch(_Agent(), object(), name="alice", task_text="x")
    # 等待完成
    await asyncio.sleep(0.01)
    assert done == [task_id]


async def test_name_registry_delegation(fake_run):
    """launch 带 name 时登记到 AgentNameRegistry。"""
    mgr = BackgroundTaskManager()
    reg = AgentNameRegistry()
    mgr.set_name_registry(reg)
    task_id = await mgr.launch(_Agent(), object(), name="alice", task_text="x")
    assert reg.resolve("alice") == task_id
    assert reg.name_of(task_id) == "alice"


async def test_on_task_done_multiple_callbacks(fake_run):
    mgr = BackgroundTaskManager()
    seen = []

    async def cb1(tid):
        seen.append("cb1")

    async def cb2(tid):
        seen.append("cb2")

    # 一个回调抛错不应影响另一个
    async def cb_bad(tid):
        raise RuntimeError("boom")

    mgr.on_task_done(cb1)
    mgr.on_task_done(cb_bad)
    mgr.on_task_done(cb2)
    await mgr.launch(_Agent(), object(), name="bob", task_text="x")
    await asyncio.sleep(0.01)
    assert "cb1" in seen and "cb2" in seen  # 坏回调被吞掉不拖垮
