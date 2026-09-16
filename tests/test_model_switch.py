"""运行时切换模型（/model，spec_model_switch）单测。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.role_loader import load_catalog
from core.tool.context import ExecutionContext
from core.tool.tools.agent_tool import AgentTool


def _p(name, protocol, model):
    return ProviderConfig(name=name, protocol=protocol, model=model, api_key="k")


class _Cfg:
    model = "x"
    context_window = 200000


class _Client:
    config = _Cfg()


def _agent() -> Agent:
    return Agent(
        registry=__import__("core.tool.registry", fromlist=["ToolRegistry"]).ToolRegistry(),
        llm_client=_Client(),
        exec_ctx=ExecutionContext(cwd=Path(tempfile.mkdtemp()), session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
        runtime=__import__("core.agent.runtime", fromlist=["SessionRuntime"]).SessionRuntime(),
    )


# ── Agent.switch_model ─────────────────────────────────────────────

def test_switch_model_changes_client_window_and_keeps_conv(monkeypatch):
    import llm.client as client_mod

    agent = _agent()
    agent._conversation.add_user_message("hello")
    before = len(agent._conversation.messages)

    new_client = object()
    monkeypatch.setattr(client_mod.LLMClient, "create", lambda cfg: new_client)
    agent.switch_model(_p("b", "openai", "m2"))

    assert agent._client is new_client  # client 换
    assert agent._prompt_builder._model == "m2"  # 模型名跟随
    assert agent._runtime.context_window == 128000  # openai 默认窗口
    assert len(agent._conversation.messages) == before  # 对话保留


def test_switch_model_updates_prompt_builder_model(monkeypatch):
    import llm.client as client_mod

    agent = _agent()
    monkeypatch.setattr(client_mod.LLMClient, "create", lambda cfg: object())
    agent.switch_model(_p("b", "anthropic", "m2"))
    assert agent._prompt_builder._model == "m2"


# ── /model 命令 ────────────────────────────────────────────────────

async def test_model_command_switch(monkeypatch):
    import core.commands.builtin_model as model_mod
    from core.commands.builtin_model import handle_model

    switched = []
    providers = [_p("a", "anthropic", "m1"), _p("b", "openai", "m2")]

    class UI:
        def providers(self):
            return providers

        def switch_model(self, p):
            switched.append(p.name)

        def error(self, m):
            raise AssertionError(m)

        def println(self, m):
            pass

        def router_cheap_tier(self):
            return None

    monkeypatch.setattr(model_mod, "select_provider", lambda ps: ps[1])
    await handle_model(UI(), "")
    assert switched == ["b"]


async def test_model_command_cancel_keeps_current(monkeypatch):
    import core.commands.builtin_model as model_mod
    from core.commands.builtin_model import handle_model

    switched = []
    providers = [_p("a", "anthropic", "m1")]

    class UI:
        def providers(self):
            return providers

        def switch_model(self, p):
            switched.append(p.name)

        def error(self, m):
            raise AssertionError(m)

        def println(self, m):
            pass

    def _cancel(ps):
        raise SystemExit(0)  # select_provider 取消

    monkeypatch.setattr(model_mod, "select_provider", _cancel)
    await handle_model(UI(), "")
    assert switched == []  # 取消不切


# ── 子 agent 用新模型 ──────────────────────────────────────────────

def test_subagent_uses_parent_client_after_switch(monkeypatch):
    import llm.client as client_mod

    agent = _agent()

    class _NewClient:
        config = _Cfg()

    new_client = _NewClient()
    monkeypatch.setattr(client_mod.LLMClient, "create", lambda cfg: new_client)
    agent.switch_model(_p("b", "anthropic", "m2"))

    tool = AgentTool(catalog=load_catalog("."), task_mgr=None, bg_enabled=True)
    tool.set_parent(agent)
    role = load_catalog(".").fork_role()
    allowed = [t.name() for t in agent._registry.list()]
    sub = tool._build_sub_agent(role, allowed, "main", is_fork=True)
    assert sub._client is new_client  # 子 agent 继承父当前 client（新模型）


async def test_model_command_cheap_disables_router_hint(monkeypatch):
    """路由启用时,切到便宜模型应提示「路由停用」,避免用户蒙在鼓里。"""
    import core.commands.builtin_model as model_mod
    from core.commands.builtin_model import handle_model

    printed = []
    providers = [
        ProviderConfig(name="cheap", protocol="anthropic", model="m", api_key="k", tier="cheap"),
        ProviderConfig(name="main", protocol="anthropic", model="m", api_key="k"),
    ]

    class UI:
        def providers(self):
            return providers

        def switch_model(self, p):
            pass

        def router_cheap_tier(self):
            return "cheap"  # 路由启用

        def error(self, m):
            raise AssertionError(m)

        def println(self, m):
            printed.append(m)

    monkeypatch.setattr(model_mod, "select_provider", lambda ps: ps[0])  # 选 cheap
    await handle_model(UI(), "")
    assert any("路由已停用" in m for m in printed)


async def test_model_command_non_cheap_no_hint(monkeypatch):
    """切到非便宜模型 → 不提示路由停用。"""
    import core.commands.builtin_model as model_mod
    from core.commands.builtin_model import handle_model

    printed = []
    providers = [
        ProviderConfig(name="cheap", protocol="anthropic", model="m", api_key="k", tier="cheap"),
        ProviderConfig(name="main", protocol="anthropic", model="m", api_key="k"),
    ]

    class UI:
        def providers(self):
            return providers

        def switch_model(self, p):
            pass

        def router_cheap_tier(self):
            return "cheap"

        def error(self, m):
            raise AssertionError(m)

        def println(self, m):
            printed.append(m)

    monkeypatch.setattr(model_mod, "select_provider", lambda ps: ps[1])  # 选 main
    await handle_model(UI(), "")
    assert not any("路由已停用" in m for m in printed)
