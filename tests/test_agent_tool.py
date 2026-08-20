"""SubAgent 集成测试 —— Agent 工具 + run_to_completion + 后台启动。

用 mock LLM 客户端驱动，验证 Agent 工具从参数解析 → 角色解析 →
子 Agent 构造 → run_to_completion 往返。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.role_loader import Catalog, load_catalog
from core.agent.runtime import SessionRuntime
from core.tool.context import ExecutionContext
from core.tool.registry import ToolRegistry

# ── mock LLM ───────────────────────────────────────────────────────


class _MockClient:
    def __init__(self, script=None, config=None) -> None:
        self._script = script or []
        self.config = config or _FakeConfig()

    async def stream_chat(
        self, messages, system_prompt="", tools=None, system_blocks=None
    ):
        # 取一轮脚本（先进先出）；无脚本时返回纯文本
        if self._script:
            step = self._script.pop(0)
        else:
            step = [{"kind": "text", "text": "final"}]

        from llm.stream_events import CompletionDone

        for item in step:
            if item.get("kind") == "text":
                from llm.stream_events import TextChunk

                yield TextChunk(text=item["text"])
            elif item.get("kind") == "tool":
                from llm.stream_events import ToolUse

                yield ToolUse(
                    id=item.get("id", "call_1"),
                    name=item["name"],
                    input=item.get("input", {}),
                    thinking="",
                )
        yield CompletionDone(usage={"input_tokens": 10, "output_tokens": 5})


class _FakeConfig:
    protocol = "anthropic"
    model = "claude-test"


# ── 工具 ───────────────────────────────────────────────────────────


class _ReadTool:
    def __init__(self, name="read_file") -> None:
        self._name = name

    def name(self):
        return self._name

    def description(self):
        return "read"

    def input_schema(self):
        return {}

    def is_read_only(self):
        return True

    def is_destructive(self):
        return False

    def is_concurrency_safe(self, input):
        return True

    def category(self):
        return "read"

    async def execute(self, context, input):
        from core.tool.result import ToolResult

        return ToolResult(success=True, data="file content")


# ── Agent 工具工厂 ─────────────────────────────────────────────────


def _build_agent_tool(catalog, task_mgr):
    from core.tool.tools.agent_tool import AgentTool

    tool = AgentTool(catalog=catalog, task_mgr=task_mgr, bg_enabled=True)
    return tool


def _make_registry() -> ToolRegistry:
    from core.tool.tools.bash import BashTool

    reg = ToolRegistry()
    reg.register(_ReadTool("read_file"))
    reg.register(_ReadTool("grep"))
    reg.register(_ReadTool("glob"))
    reg.register(BashTool())
    return reg


def _make_parent_agent(registry) -> Agent:
    conv = ConversationManager()
    runtime = SessionRuntime()
    exec_ctx = ExecutionContext(cwd=Path.cwd(), session_id="main")
    client = _MockClient()
    return Agent(
        registry=registry,
        llm_client=client,
        exec_ctx=exec_ctx,
        conversation=conv,
        config=AgentConfig(max_iterations=5),
        runtime=runtime,
    )


# ── 测试 ───────────────────────────────────────────────────────────


def test_agent_tool_parameters():
    from core.tool.tools.agent_tool import AgentTool

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)
    assert tool.name() == "Agent"
    schema = tool.input_schema()
    props = schema["properties"]
    for key in (
        "prompt",
        "description",
        "subagent_type",
        "model",
        "run_in_background",
        "name",
    ):
        assert key in props
    assert "prompt" in schema["required"]
    assert "description" in schema["required"]


def test_agent_tool_missing_prompt():
    from core.tool.tools.agent_tool import AgentTool

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)
    result = asyncio.run(tool.execute(_ctx("main"), {"description": "test"}))
    assert not result.success
    assert "prompt is required" in result.error


def test_agent_tool_unknown_subagent_type():
    from core.tool.tools.agent_tool import AgentTool

    cat = Catalog()
    tool = AgentTool(catalog=cat, task_mgr=None, bg_enabled=True)
    result = asyncio.run(
        tool.execute(
            _ctx("main"),
            {"prompt": "hi", "description": "test", "subagent_type": "nonexistent"},
        )
    )
    assert not result.success
    assert "Unknown subagent_type" in result.error


def _ctx(session_id):
    return ExecutionContext(cwd=Path.cwd(), session_id=session_id)


def test_agent_tool_catalog_resolves():
    """定义式子 Agent 调用走前台 run_to_completion 返回 final_text。"""
    from core.tool.tools.agent_tool import AgentTool

    cat = load_catalog(str(Path.cwd()))
    assert cat.resolve("Explore") is not None
    tool = AgentTool(catalog=cat, task_mgr=None, bg_enabled=True)
    # description 动态列出角色
    desc = tool.description()
    assert "Explore" in desc


def test_agent_tool_concurrency_safety():
    """并行安全：worktree 隔离 OR 只读角色 → 安全；共享工作区可写 → 不安全。"""
    from core.agent.roles import AgentRole
    from core.tool.tools.agent_tool import AgentTool

    cat = load_catalog(str(Path.cwd()))
    # 追加一个 worktree 隔离的可写角色，验证 isolation → 安全
    cat._add_all([
        AgentRole(
            name="coder-isolated",
            description="isolated coder",
            isolation=True,
        ),
    ])
    tool = AgentTool(catalog=cat, task_mgr=None, bg_enabled=True)

    # 只读角色（Explore 禁写文件）→ 安全
    assert tool.is_concurrency_safe({"subagent_type": "Explore"}) is True
    # worktree 隔离的可写角色 → 安全
    assert tool.is_concurrency_safe({"subagent_type": "coder-isolated"}) is True
    # 共享工作区 + 可写（general-purpose 无隔离无禁写）→ 不安全
    assert tool.is_concurrency_safe({"subagent_type": "general-purpose"}) is False
    # fork（无 subagent_type）/ 未知类型 → 保守串行
    assert tool.is_concurrency_safe({}) is False
    assert tool.is_concurrency_safe({"subagent_type": "nope"}) is False


@pytest.mark.asyncio
async def test_agent_tool_background_launch():
    """run_in_background=True → 返回 async_launched JSON。"""
    from core.task.manager import BackgroundTaskManager
    from core.tool.tools.agent_tool import AgentTool

    catalog = load_catalog(str(Path.cwd()))
    task_mgr = BackgroundTaskManager()
    from core.tool.tools.agent_tool import AgentTool

    registry = _make_registry()
    parent = _make_parent_agent(registry)
    tool = AgentTool(catalog=catalog, task_mgr=task_mgr, bg_enabled=True)
    tool.set_parent(parent)

    result = await tool.execute(
        _ctx("main"),
        {
            "prompt": "count the files",
            "description": "count files",
            "subagent_type": "Explore",
            "run_in_background": True,
        },
    )
    assert result.success
    assert "async_launched" in result.data
    assert "task_" in result.data


@pytest.mark.asyncio
async def test_agent_tool_fork_background():
    """Fork 路径（无 subagent_type）→ 强制后台。"""
    from core.task.manager import BackgroundTaskManager
    from core.tool.tools.agent_tool import AgentTool

    catalog = load_catalog(str(Path.cwd()))
    task_mgr = BackgroundTaskManager()
    from core.tool.tools.agent_tool import AgentTool

    registry = _make_registry()
    parent = _make_parent_agent(registry)
    tool = AgentTool(catalog=catalog, task_mgr=task_mgr, bg_enabled=True)
    tool.set_parent(parent)

    # 父对话铺垫
    parent._conversation.add_user_message("hello")

    result = await tool.execute(
        _ctx("main"),
        {"prompt": "summarize", "description": "summarize"},
    )
    assert result.success
    assert "async_launched" in result.data  # Fork 强制后台


def test_agent_tool_bg_disabled_fork_error():
    """enable_subagent_background=False 时 Fork 报错。"""
    from core.task.manager import BackgroundTaskManager
    from core.tool.tools.agent_tool import AgentTool

    catalog = load_catalog(str(Path.cwd()))
    task_mgr = BackgroundTaskManager()
    from core.tool.tools.agent_tool import AgentTool

    registry = _make_registry()
    parent = _make_parent_agent(registry)
    tool = AgentTool(catalog=catalog, task_mgr=task_mgr, bg_enabled=False)
    tool.set_parent(parent)

    result = asyncio.run(
        tool.execute(
            _ctx("main"),
            {"prompt": "summarize", "description": "summarize"},
        )
    )
    assert not result.success
    assert "background" in result.error


@pytest.mark.asyncio
async def test_run_to_completion_basic():
    """run_to_completion 基本往返：mock client 返回文本。"""
    from core.agent.sub_agent import run_to_completion

    registry = _make_registry()
    agent = _make_parent_agent(registry)
    # 替换 client 的 script 为固定文本
    agent._client._script = [[{"kind": "text", "text": "hello subagent"}]]

    conv = ConversationManager()
    result = await run_to_completion(agent, conv, "do task")
    assert "hello subagent" in result


@pytest.mark.asyncio
async def test_agent_tool_isolation_creates_worktree(tmp_path):
    """isolation 角色 → 创建 worktree，子 Agent cwd 指向 worktree。"""
    import subprocess

    from core.agent.roles import AgentRole, Source
    from core.task.manager import BackgroundTaskManager
    from core.tool.tools.agent_tool import AgentTool
    from core.worktree.manager import WorktreeManager

    # 初始化真实 git 仓库
    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (
        ["init"],
        ["config", "user.email", "t@t.com"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    (repo / "f.txt").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True
    )

    # 构造 isolation 角色
    role = AgentRole(
        name="iso",
        description="iso",
        isolation=True,
        system_prompt="work isolated",
        source=Source.PROJECT,
    )
    cat = Catalog()
    cat._defs["iso"] = role

    registry = _make_registry()
    parent = _make_parent_agent(registry)
    parent._exec_ctx.cwd = repo

    wt_mgr = WorktreeManager(str(repo))
    task_mgr = BackgroundTaskManager()
    tool = AgentTool(catalog=cat, task_mgr=task_mgr, bg_enabled=True, wt_manager=wt_mgr)
    tool.set_parent(parent)

    result = await tool.execute(
        _ctx("main"),
        {
            "prompt": "do it",
            "description": "test",
            "subagent_type": "iso",
            "run_in_background": True,
        },
    )
    assert result.success, result.error

    # worktree 已创建并注册
    sessions = wt_mgr.list_active()
    assert len(sessions) == 1
    wt_path = Path(sessions[0].path)
    assert wt_path.is_dir()
    # 子 Agent 的 cwd 指向 worktree（在后台 launch 用的 task 中验证）
    tasks = task_mgr.list()
    assert len(tasks) == 1
    assert str(tasks[0].sub_agent._exec_ctx.cwd) == str(wt_path)


# ── 回归：Fork 子 Agent 创建时即自主（bypass），执行不卡 HITL ───────


def test_fork_subagent_is_autonomous_bypass(monkeypatch):
    """fork 子 Agent 创建时 permission=BYPASS + dont_ask=True（对齐参考实现）。

    回归背景：多智能体实测中发现默认 fork 子 Agent permission_mode=DEFAULT，
    其 write/bash 触发 HITL ask 而在 _hitl_event.wait() 上永远挂起、写不出文件。
    """
    from core.tool.tools.agent_tool import AgentTool

    registry = _make_registry()
    parent = _make_parent_agent(registry)
    tool = AgentTool(catalog=load_catalog("."), task_mgr=None, bg_enabled=True)
    tool.set_parent(parent)

    # fork 角色（subagent_type 为空 → fork_role）
    role = load_catalog(".").fork_role()
    # 构造完整工具列表（fork 保留 Agent 工具，这里只取父注册表工具名）
    allowed = [t.name() for t in registry.list()]

    sub = tool._build_sub_agent(role, allowed, "main", is_fork=True)

    assert sub.permission_mode.value == "bypassPermissions"
    assert sub.dont_ask is True


def test_defined_role_subagent_keeps_own_permission():
    """定义式角色子 Agent 尊重其角色声明的权限模式（非 fork 不强加 bypass）。"""
    from core.tool.tools.agent_tool import AgentTool

    registry = _make_registry()
    parent = _make_parent_agent(registry)
    tool = AgentTool(catalog=load_catalog("."), task_mgr=None, bg_enabled=True)
    tool.set_parent(parent)

    # 造一个声明 own permission 的假角色（如 Explore 只读，非 fork）
    from core.agent.roles import AgentRole
    from core.permissions.modes import PermissionMode

    role = AgentRole(
        name="myrole",
        system_prompt="",
        description="x",
        max_turns=5,
        permission_mode=PermissionMode.DEFAULT,
        dont_ask=False,
    )
    allowed = [t.name() for t in registry.list()]
    sub = tool._build_sub_agent(role, allowed, "main", is_fork=False)

    # 非 fork：保留角色声明的 default（不强改 bypass）
    assert sub.permission_mode.value == "default"
    assert sub.dont_ask is False


def test_subagent_model_override_applied():
    """子代理指定非 inherit 模型 → 其客户端 config.model 被覆盖。"""
    from config.model import ProviderConfig
    from core.agent.agent import Agent
    from core.agent.config import AgentConfig
    from core.agent.runtime import SessionRuntime
    from conversation.manager import ConversationManager
    from core.tool.context import ExecutionContext
    from core.tool.tools.agent_tool import AgentTool

    registry = _make_registry()
    parent_client = ProviderConfig(
        name="t", protocol="anthropic", model="parent-model", api_key="sk-x"
    )
    parent = Agent(
        registry=registry,
        llm_client=__import__("llm.client", fromlist=["LLMClient"]).LLMClient.create(
            parent_client
        ),
        exec_ctx=ExecutionContext(cwd=Path.cwd(), session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=5),
        runtime=SessionRuntime(),
    )
    tool = AgentTool(catalog=load_catalog("."), task_mgr=None, bg_enabled=True)
    tool.set_parent(parent)

    allowed = [t.name() for t in registry.list()]
    # 指定 haiku 模型
    sub = tool._build_sub_agent(
        None, allowed, "main", is_fork=False, model="haiku"
    )
    assert sub._client.config.model == "haiku"

    # 不指定（inherit）→ 继承父模型
    sub_inherit = tool._build_sub_agent(None, allowed, "main", is_fork=False, model="")
    assert sub_inherit._client.config.model == "parent-model"


# ── 回归：前台子 Agent 同步 await（对齐参考 mewcode，消除 sleep 轮询）──


async def test_foreground_subagent_sync_no_timeout(monkeypatch):
    """前台子 agent 同步等到结果文本，不设超时、不自动转后台。"""
    from core.tool.tools.agent_tool import AgentArgs, AgentTool

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)

    async def _fake_rtc(agent, conv, prompt, events=None):
        return "vowels-worker result: count_vowels done"

    monkeypatch.setattr("core.agent.sub_agent.run_to_completion", _fake_rtc)
    args = AgentArgs(prompt="write vowels.py", description="t")
    res = await tool._run_foreground(object(), object(), args)
    assert res.success
    assert res.data == "vowels-worker result: count_vowels done"


def test_foreground_has_no_timeout_constant(monkeypatch):
    """_run_foreground 不再依赖 AUTO_BACKGROUND_SECONDS/wait_for 超时转后台。"""
    import inspect

    from core.tool.tools import agent_tool
    from core.tool.tools.agent_tool import AgentTool

    src = inspect.getsource(AgentTool._run_foreground)
    assert "wait_for" not in src
    assert "AUTO_BACKGROUND_SECONDS" not in src
    assert "timed_out_to_background" not in src
    assert "adopt_running" not in src


# ── 回归：子 agent 只读继承会话状态（spec_session_state）────────────

def test_subagent_inherits_state_snapshot_readonly(monkeypatch, tmp_path):
    """子 agent 的 system 含父会话目标+约束快照；registry 无状态写工具。"""
    from core.notes.state import SessionStateStore
    from core.tool.tools.agent_tool import AgentTool

    store = SessionStateStore(tmp_path / "sess")
    store.set_goal("修 bug")
    store.add_constraint("别改 test.py")

    registry = _make_registry()
    # 把状态工具注册进父 registry
    from core.tool.tools.state_tool import register_state_tools

    register_state_tools(registry, store)

    parent = _make_parent_agent(registry)
    parent.set_state_store(store)

    tool = AgentTool(catalog=load_catalog("."), task_mgr=None, bg_enabled=True)
    tool.set_parent(parent)

    role = load_catalog(".").fork_role()
    allowed = [t.name() for t in registry.list()]
    sub = tool._build_sub_agent(role, allowed, "main", is_fork=True)

    # 快照注入
    sysp = sub._system_prompt_override or ""
    assert "修 bug" in sysp
    assert "别改 test.py" in sysp

    # 无状态写工具（只读继承）
    sub_names = {t.name() for t in sub._registry.list()}
    assert not (sub_names & {"SetGoal", "AddTodo", "AddConstraint"})


async def test_foreground_error_text_marks_failure(monkeypatch):
    """子 agent 返回 Error: 文本 → 结构化 success=False（委派失败显式化）。"""
    from core.tool.tools.agent_tool import AgentArgs, AgentTool

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)

    async def _fake_rtc(agent, conv, prompt, events=None):
        return "Error: stream failed mid-way"

    monkeypatch.setattr("core.agent.sub_agent.run_to_completion", _fake_rtc)
    res = await tool._run_foreground(
        object(), object(), AgentArgs(prompt="x", description="t")
    )
    assert res.success is False
    assert "Error" in res.error


async def test_foreground_normal_text_is_success(monkeypatch):
    """子 agent 正常完成 → success=True。"""
    from core.tool.tools.agent_tool import AgentArgs, AgentTool

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)

    async def _fake_rtc(agent, conv, prompt, events=None):
        return "fixed calc.py"

    monkeypatch.setattr("core.agent.sub_agent.run_to_completion", _fake_rtc)
    res = await tool._run_foreground(
        object(), object(), AgentArgs(prompt="x", description="t")
    )
    assert res.success is True
    assert res.data == "fixed calc.py"
