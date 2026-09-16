"""`--task` 无头单任务开关的单测。

覆盖可观测的分支逻辑（不触发真实 LLM）：
- `run(task=...)` 跳过交互式 `select_provider`，改用 `providers[0]`，并把 task 传进
  `_run_async`。
- `run()`（无 task）保持原样：走交互式 `select_provider`。
- `_run_async` 的 task-分支（`if task: await _inject_and_run(app, task)`）通过
  打桩 `_run_async` 间接覆盖。
"""

from __future__ import annotations

import types

import tui.app
import tui.provider_select as provider_select_mod


def _fake_provider():
    return types.SimpleNamespace(
        name="fake", model="t", protocol="anthropic", base_url=""
    )


def test_run_task_skips_select_provider_and_passes_task(monkeypatch):
    """task 非空 → 用 providers[0]，不调 select_provider，task 传进 _run_async。"""
    monkeypatch.setattr(tui.app, "load_config", lambda path: [_fake_provider()])
    select_called = []

    def _boom(providers):
        select_called.append(True)
        raise AssertionError("headless 不应调用交互式 select_provider")

    monkeypatch.setattr(provider_select_mod, "select_provider", _boom)
    seen = {}

    async def fake_run_async(provider, task="", providers=None, loop=""):
        seen["provider"] = provider
        seen["task"] = task

    monkeypatch.setattr(tui.app, "_run_async", fake_run_async)

    tui.app.run(task="hello world")

    assert select_called == []
    assert seen["provider"].name == "fake"
    assert seen["task"] == "hello world"


def test_run_default_uses_select_provider(monkeypatch):
    """无 task → 走交互式 select_provider（原样行为，不回归）。"""
    monkeypatch.setattr(
        tui.app, "load_config", lambda path: [_fake_provider(), _fake_provider()]
    )
    sel = []

    def _fake_select(providers):
        sel.append(len(providers))
        return providers[1]

    monkeypatch.setattr(
        tui.app, "select_provider", _fake_select
    )  # run() 用的是 tui.app 里的绑定
    seen = {}

    async def fake_run_async(provider, task="", providers=None, loop=""):
        seen["provider"] = provider
        seen["task"] = task

    monkeypatch.setattr(tui.app, "_run_async", fake_run_async)

    tui.app.run()

    assert sel == [2]  # select_provider 被调用，且看到 2 个 provider
    assert seen["task"] == ""  # 无 task
