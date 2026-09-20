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

                # 注意：`ToolUse` 只有 id/name/input 三个字段。
                # 这里曾多传一个 `thinking=""` → 每次走到这条分支都抛 TypeError，
                # 被 `_run_loop` 的宽 except 吞成 error_event，于是"脚本里的工具"
                # 从来没真正产出过 tool_use（text-only 兜底掩盖了它）。
                yield ToolUse(
                    id=item.get("id", "call_1"),
                    name=item["name"],
                    input=item.get("input", {}),
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


# ── 回归：Fork 子 Agent 自主干活，但不靠"无条件放行"实现 ─────────────


def test_fork_subagent_is_autonomous_without_bypass(monkeypatch):
    """fork 子 Agent 自主干活，但**不再无条件 BYPASS**。

    历史背景：早期 fork 子 Agent 是 `permission_mode=DEFAULT`，其 write/bash
    触发 HITL ask 后卡在 `_hitl_event.wait()` 永远挂起、写不出文件。当时的修法
    是给它 `BYPASS` + `dont_ask=True` —— 但那让子 Agent 拿到**比父更大**的授权，
    而且 BYPASS 会让决策直接落到 allow、绕开无人值守策略（D4）。

    现在的修法：用无人值守策略代答 ask（**不再挂起**），授权按父推导 ——
    父是默认模式且无显式策略时取 `allow_write`（能干活、命令仍拒）。
    """
    from core.permissions.modes import PermissionMode as PM
    from core.permissions.modes import UnattendedPolicy
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

    assert sub.permission_mode is not PM.BYPASS, "不再无条件 BYPASS"
    assert sub.dont_ask is False, "不再走『一律 allow』的老路"
    assert sub.unattended_policy is UnattendedPolicy.ALLOW_WRITE


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


# ── 子 Agent 策略继承（D4）────────────────────────────────────────────
#
# 回归背景：fork 子 Agent 原先无条件 `permission_mode=BYPASS` + `dont_ask=True`
# （理由是"自主干完、不逐工具 ask"）。那让子 Agent 拿到**比父更大**的授权，
# 而且 BYPASS 会让决策直接落在 allow、根本进不到 ask —— 使无人值守策略形同虚设。
# 改为：模式继承父 + 策略按父的授权范围推导（`resolve_child_policy`）。


def _fork_child(parent):
    from core.tool.tools.agent_tool import AgentTool

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=False)
    tool.set_parent(parent)
    return tool._build_sub_agent(
        role=None, allowed=["write_file", "read_file"], session_id="main", is_fork=True
    )


def _write_effect(agent) -> str:
    from llm.stream_events import ToolUse

    return agent._check_tool_permission(
        ToolUse(id="tu", name="write_file", input={"path": "a.txt", "content": "x"})
    ).effect


def test_fork_child_cannot_write_under_deny_all_parent():
    """父设 deny_all → fork 子 Agent 不得写文件（checklist D4 的明文要求）。"""
    from core.permissions.modes import UnattendedPolicy
    from core.tool.tools import get_default_registry

    parent = _make_parent_agent(get_default_registry())
    parent.set_unattended_policy("deny_all")

    sub = _fork_child(parent)

    assert sub.unattended_policy is UnattendedPolicy.DENY_ALL
    assert sub._dont_ask is False, "不再走『一律 allow』的老路"
    assert _write_effect(sub) == "deny"


def test_fork_child_aligns_with_bypass_parent():
    """父在 BYPASS 全放行模式 → 子也全放行（授权对齐，不多也不少）。"""
    from core.permissions.modes import PermissionMode as PM
    from core.permissions.modes import UnattendedPolicy
    from core.tool.tools import get_default_registry

    parent = _make_parent_agent(get_default_registry())
    parent.set_permission_mode(PM.BYPASS)

    sub = _fork_child(parent)

    assert sub.unattended_policy is UnattendedPolicy.ALLOW_ALL
    assert _write_effect(sub) == "allow"


def test_fork_child_gets_allow_write_when_parent_has_no_policy():
    """父未设策略且在默认模式 → 子取 `allow_write`（能干活、命令仍拒）。

    为什么不取 `deny_all`：父自己写文件也要经人批准，说明"写"是用户认可的意图，
    子 Agent 无人可问时取 `allow_write` 最贴近它；而**命令执行仍拒**，
    相对早先的无条件 BYPASS 是实打实的收紧。
    """
    from core.permissions.modes import UnattendedPolicy
    from core.tool.tools import get_default_registry

    parent = _make_parent_agent(get_default_registry())

    sub = _fork_child(parent)

    assert sub.unattended_policy is UnattendedPolicy.ALLOW_WRITE
    assert _write_effect(sub) == "allow"


def test_fork_child_no_longer_bypass_mode():
    """fork 不再无条件 BYPASS —— 否则会绕过无人值守策略。"""
    from core.permissions.modes import PermissionMode as PM
    from core.tool.tools import get_default_registry

    parent = _make_parent_agent(get_default_registry())
    sub = _fork_child(parent)

    assert sub.permission_mode is not PM.BYPASS


def test_role_dontask_translated_to_allow_all_policy():
    """角色的 `permissionMode: dontask` 契约（"ask 自动放行"）必须兑现。

    做法是把该契约**翻译成策略** `allow_all`。若任由 `_dont_ask` 与策略并存，
    由于策略在权限判定里优先，角色契约会静默失效 —— 这条把翻译关系钉住。
    """
    from core.permissions.modes import PermissionMode as PM
    from core.permissions.modes import UnattendedPolicy
    from core.tool.tools import get_default_registry
    from core.tool.tools.agent_tool import AgentTool

    class _Role:
        permission_mode = PM.DEFAULT
        dont_ask = True
        max_turns = 5
        system_prompt = ""

    # 父是默认模式且未设策略 → 若走推导本会是 deny_all；角色的 dontask 应压过它
    parent = _make_parent_agent(get_default_registry())

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=False)
    tool.set_parent(parent)
    sub = tool._build_sub_agent(
        role=_Role(), allowed=["write_file"], session_id="main", is_fork=False
    )

    assert sub.unattended_policy is UnattendedPolicy.ALLOW_ALL
    assert _write_effect(sub) == "allow", "角色声明 dontask ⇒ 写操作自动放行"


def test_role_without_dontask_follows_parent_policy():
    """未声明 dontask 的定义式角色子 Agent 按父的授权范围走（不再一律放行）。

    父是默认模式 → 子取 `allow_write`：写放行、命令仍拒。
    """
    from core.permissions.modes import PermissionMode as PM
    from core.permissions.modes import UnattendedPolicy
    from core.tool.tools import get_default_registry
    from core.tool.tools.agent_tool import AgentTool

    class _Role:
        permission_mode = PM.DEFAULT
        dont_ask = False
        max_turns = 5
        system_prompt = ""

    parent = _make_parent_agent(get_default_registry())

    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=False)
    tool.set_parent(parent)
    sub = tool._build_sub_agent(
        role=_Role(), allowed=["write_file"], session_id="main", is_fork=False
    )

    assert sub.unattended_policy is UnattendedPolicy.ALLOW_WRITE


# ── 审批升级通道的前后台分流（spec_subagent.md 附录 A.5.2）─────────────
#
# 规格：前台子 Agent 的 ask 冒泡到主 TUI；后台不冒泡，保持策略代答。
# 这里钉住最要紧的一条接缝：子 Agent 建的时候**已经挂了策略**（allow_write），
# 不摘掉策略，请求在权限层就被代答掉了、通道根本轮不到 —— 功能会是死的。


class _ScriptedClient:
    """按脚本产出：先调一次 write_file，再收尾。"""

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self._script = [
            [
                {
                    "kind": "tool",
                    "name": "write_file",
                    "input": {"file_path": "probe_out.txt", "content": "x"},
                }
            ],
            [{"kind": "text", "text": "done"}],
        ]

    async def stream_chat(
        self, messages, system_prompt="", tools=None, system_blocks=None
    ):
        from llm.stream_events import CompletionDone, TextChunk, ToolUse

        step = (
            self._script.pop(0) if self._script else [{"kind": "text", "text": "done"}]
        )
        for item in step:
            if item.get("kind") == "text":
                yield TextChunk(text=item["text"])
            else:
                yield ToolUse(id="c1", name=item["name"], input=item.get("input", {}))
        yield CompletionDone(usage={"input_tokens": 5, "output_tokens": 2})


def _plain_role():
    """默认模式、不声明 dontask 的定义式角色。"""
    from core.agent.roles import AgentRole
    from core.permissions.modes import PermissionMode

    return AgentRole(
        name="roleguy",
        description="x",
        max_turns=5,
        permission_mode=PermissionMode.DEFAULT,
        dont_ask=False,
    )


def _parent_with_upgrader(cwd, upgrader) -> Agent:
    from core.permissions.modes import PermissionMode
    from core.tool.tools import get_default_registry

    return Agent(
        registry=get_default_registry(),
        llm_client=_ScriptedClient(),
        exec_ctx=ExecutionContext(
            cwd=Path(cwd), session_id="main", approval_upgrader=upgrader
        ),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=5),
        runtime=SessionRuntime(),
        permission_mode=PermissionMode.DEFAULT,
    )


@pytest.mark.asyncio
async def test_foreground_subagent_escalates_approval(tmp_path):
    """前台：请求冒泡到通道并带来源；放行后写盘；跑完通道摘掉、策略还原。"""
    from core.permissions.hitl import HITLChoice, HITLRequest, HITLResponse
    from core.permissions.upgrade import ApprovalUpgrader
    from core.tool.tools.agent_tool import AgentArgs, AgentTool

    up = ApprovalUpgrader()
    seen: list[HITLRequest] = []

    def handler(req: HITLRequest) -> HITLResponse:
        seen.append(req)
        return HITLResponse(choice=HITLChoice.ALLOW_ONCE, tool_name=req.tool_name)

    up.start(handler)
    parent = _parent_with_upgrader(tmp_path, up)
    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)
    tool.set_parent(parent)
    sub = tool._build_sub_agent(
        role=_plain_role(),
        allowed=["write_file"],
        session_id="main",
        is_fork=False,
        name="calc",
    )
    # 前提：建完就有策略 —— 正因如此，`_run_foreground` 必须把它摘掉
    policy_before = sub.unattended_policy
    assert policy_before is not None

    try:
        result = await tool._run_foreground(
            sub, ConversationManager(), AgentArgs(prompt="do it")
        )
    finally:
        await up.stop()

    assert len(seen) == 1, "前台子 Agent 的 ask 必须冒泡到通道"
    assert seen[0].origin == "calc", "请求必须带上来源身份"
    assert result.success
    assert (sub._exec_ctx.cwd / "probe_out.txt").exists(), "放行后工具应执行"
    assert sub._approval_upgrader is None, "跑完必须摘掉通道（实例可能被续派复用）"
    assert sub.unattended_policy is policy_before, "策略必须还原"


@pytest.mark.asyncio
async def test_background_subagent_does_not_escalate(tmp_path):
    """后台：**不**冒泡（父已继续跑，没有可弹窗的时机），保持策略代答。"""
    import json

    from core.permissions.hitl import HITLChoice, HITLRequest, HITLResponse
    from core.permissions.upgrade import ApprovalUpgrader
    from core.task.manager import BackgroundTaskManager
    from core.tool.tools.agent_tool import AgentArgs, AgentTool

    up = ApprovalUpgrader()
    seen: list[HITLRequest] = []

    def handler(req: HITLRequest) -> HITLResponse:
        seen.append(req)
        return HITLResponse(choice=HITLChoice.DENY, tool_name=req.tool_name)

    up.start(handler)
    parent = _parent_with_upgrader(tmp_path, up)
    mgr = BackgroundTaskManager()
    done_q = mgr.subscribe_done()
    tool = AgentTool(catalog=None, task_mgr=mgr, bg_enabled=True)
    tool.set_parent(parent)
    sub = tool._build_sub_agent(
        role=_plain_role(),
        allowed=["write_file"],
        session_id="main",
        is_fork=False,
        name="bgcalc",
    )

    try:
        result = await tool._run_background(
            sub, ConversationManager(), AgentArgs(prompt="do it", name="bgcalc")
        )
        json.loads(result.data)  # 确认是 async_launched 结构
        await asyncio.wait_for(done_q.get(), timeout=20)
    finally:
        await up.stop()

    assert seen == [], "后台子 Agent 不该弹审批（消费者在跑也不该被问到）"
    # 策略代答放行写操作 → 文件仍应写出，行为与今天一致
    assert (sub._exec_ctx.cwd / "probe_out.txt").exists()


# ── §1.7 run_to_completion 路径（此前零覆盖：max_turns / events / 取消）──


def _scripted_agent(script, *, max_turns=5, hooks=None) -> Agent:
    """带脚本化 LLM 的真实 Agent（复用 _make_parent_agent 的装配）。"""
    agent = _make_parent_agent(_make_registry())
    agent._client._script = list(script)
    agent._max_turns = max_turns
    if hooks is not None:
        agent._hooks = hooks
    return agent


@pytest.mark.asyncio
async def test_run_to_completion_reaches_max_turns_returns_last_text():
    """触达 max_turns → 返回累计 assistant 文本，**不抛异常**。

    清单 §1.7「触达 max_turns → 返回最后 assistant 文本」。
    注意 `run_to_completion` 的 docstring 写的是 `Raises: MaxTurnsReached`，
    代码实际是 `return` —— 以代码为准（docstring 与实现不一致，已在清单里记一笔）。
    """
    from core.agent.sub_agent import run_to_completion

    # 每轮都带一个 tool_use，循环永远不会自然终止 → 必然走 max_turns 出口
    step = [{"kind": "text", "text": "t"}, {"kind": "tool", "name": "read_file"}]
    agent = _scripted_agent([list(step), list(step), list(step)], max_turns=2)

    result = await run_to_completion(agent, ConversationManager(), "go")
    assert result == "tt", f"两轮各累计一个 't'，期望 'tt'，实际 {result!r}"


@pytest.mark.asyncio
async def test_run_to_completion_forwards_events_to_queue():
    """events 队列转发：text / tools_start / tool 三类都能被外部消费到。"""
    from core.agent.sub_agent import run_to_completion

    agent = _scripted_agent(
        [[{"kind": "text", "text": "hi"}, {"kind": "tool", "name": "read_file"}]],
        max_turns=3,
    )
    q: asyncio.Queue = asyncio.Queue()

    await run_to_completion(agent, ConversationManager(), "go", q)

    kinds: list[str] = []
    while not q.empty():
        kinds.append(q.get_nowait()[0])
    assert "text" in kinds, f"未转发 text 事件，实际 {kinds}"
    assert "tools_start" in kinds, f"未转发 tools_start 事件，实际 {kinds}"
    assert "tool" in kinds, f"未转发 tool 事件，实际 {kinds}"


@pytest.mark.asyncio
async def test_run_to_completion_propagates_cancelled_error():
    """取消标记已置位 → 首轮即抛 CancelledError（清单 §1.7「透传」）。"""
    from core.agent.sub_agent import run_to_completion

    agent = _scripted_agent([[{"kind": "text", "text": "x"}]])
    agent._cancel.set()

    with pytest.raises(asyncio.CancelledError):
        await run_to_completion(agent, ConversationManager(), "go")


# ── §1.8 Fork 上下文的工具层拦截（此前只测了 is_fork_context 本身）──


@pytest.mark.asyncio
async def test_agent_tool_blocks_spawn_from_fork_context():
    """父对话含 fork boilerplate → Agent 工具拒绝派生（§1.8）。

    此前 `is_fork_context` 有单测，但「工具层真的拿它拦人」这条接线无人守。
    """
    from core.agent.fork import build_forked_messages
    from core.tool.tools.agent_tool import AgentTool

    tool = AgentTool(
        catalog=load_catalog(str(Path.cwd())), task_mgr=None, bg_enabled=True
    )
    parent = _make_parent_agent(_make_registry())
    parent._conversation.replace_history(build_forked_messages([], "prior task"))
    tool.set_parent(parent)

    result = await tool.execute(_ctx("main"), {"prompt": "nested", "description": "d"})
    assert not result.success, "Fork 上下文里派子 Agent 必须被拦"
    assert "boilerplate" in result.error, f"错误信息应点名 boilerplate：{result.error}"


# ── §3 hook 引擎在子 Agent 内仍生效 ────────────────────────────────


def test_subagent_inherits_parent_hooks():
    """接缝：`_build_sub_agent` 把父的 hook 引擎传给子 Agent。

    不继承 = 子 Agent 里所有 PreToolUse/PostToolUse 静默失效。
    """
    from core.hooks.runner import HookRunner
    from core.tool.tools.agent_tool import AgentTool

    runner = HookRunner([], cwd=Path.cwd())
    parent = _make_parent_agent(_make_registry())
    parent._hooks = runner
    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)
    tool.set_parent(parent)

    sub = tool._build_sub_agent(_plain_role(), ["read_file"], "main", is_fork=False)
    assert sub._hooks is runner, "子 Agent 必须继承父的 hook 引擎"


@pytest.mark.asyncio
async def test_hook_engine_blocks_tool_inside_subagent(tmp_path):
    """功能面：blocking pre_tool hook 真的能在子 Agent 内拦住工具。

    反证（同一条调用在无 hook 时工具照常执行）——否则「被拦了」可能是别的原因
    （比如权限拒绝、工具不存在）。
    """
    from core.hooks.rules import HookAction, HookRule
    from core.hooks.runner import HookRunner
    from core.tool.tools import get_default_registry
    from core.tool.tools.agent_tool import AgentTool
    from llm.stream_events import ToolUse

    target = tmp_path / "probe.txt"
    target.write_text("hello", encoding="utf-8")

    parent = _make_parent_agent(get_default_registry())

    def _sub_with(hooks):
        parent._hooks = hooks
        tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=True)
        tool.set_parent(parent)
        return tool._build_sub_agent(_plain_role(), ["read_file"], "main", is_fork=False)

    async def _run_and_collect(sub) -> tuple[str, list[str]]:
        """跑一次工具，返回 (ToolCallFinished.preview, 写进对话的 tool_result 文本)。

        注意被 hook 拦截时 `_execute_tools` **不产出 ToolCallFinished**（该调用
        在预检阶段就被记进 results，随后被 `allowed_tus` 过滤掉），但对话历史里
        仍会补一条 `Error: Blocked by hook: ...` 的 tool_result——否则下一轮 API
        调用会带着悬空 tool_use。
        """
        from conversation.message import MessageRole

        preview = ""
        async for ev in sub._execute_tools(
            [ToolUse(id="t", name="read_file", input={"file_path": str(target)})]
        ):
            if type(ev).__name__ == "ToolCallFinished":
                preview = ev.result_preview
        results = [
            m.content
            for m in sub._conversation.messages
            if m.role == MessageRole.USER and m.tool_use_id
        ]
        return preview, results

    # 反证：无 hook → 真的读到了文件内容
    plain_preview, plain_results = await _run_and_collect(_sub_with(None))
    assert "hello" in plain_preview, f"无 hook 时 read_file 应成功，实际 {plain_preview!r}"
    assert plain_results and "hello" in plain_results[-1], (
        f"无 hook 时对话应写入文件内容，实际 {plain_results!r}"
    )

    # 有 hook：pre_tool 退出码非 0 = 拦截
    runner = HookRunner(
        [
            HookRule(
                name="block-all",
                event="pre_tool",
                action=HookAction(type="command", command="exit 1"),
            )
        ],
        cwd=Path.cwd(),
    )
    hooked_preview, hooked_results = await _run_and_collect(_sub_with(runner))
    assert hooked_preview == "", f"被拦截的调用不该产出 ToolCallFinished，实际 {hooked_preview!r}"
    assert hooked_results and hooked_results[-1].startswith("Error: Blocked by hook"), (
        f"对话里应记下被拦截原因，实际 {hooked_results!r}"
    )
