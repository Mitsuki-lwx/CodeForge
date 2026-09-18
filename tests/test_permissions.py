"""权限系统的分类归一化与模式决策测试。

重点覆盖 `normalize_tool_category`：`file` 是读写工具共用的 category，
必须靠 `is_read_only` 区分。此前写文件被兜底成 `command`，导致
`acceptEdits` 模式对写文件失效（矩阵是 write→allow，实际走 command→ask）。
"""

from __future__ import annotations

import pytest

from core.agent.agent import normalize_tool_category
from core.permissions.checker import PermissionChecker
from core.permissions.modes import PermissionMode
from core.tool.tools import get_default_registry

# ── 分类归一化 ─────────────────────────────────────────────────────


def test_file_category_splits_by_read_only():
    """`file` 必须按 is_read_only 分流：只读→read，可写→write。"""
    assert normalize_tool_category("file", True) == "read"
    assert normalize_tool_category("file", False) == "write"


def test_read_file_maps_to_read():
    """read_file（category=file, 只读）归为 read。"""
    t = get_default_registry().get("read_file")
    assert t.category() == "file"
    assert normalize_tool_category(t.category(), t.is_read_only()) == "read"


@pytest.mark.parametrize("name", ["write_file", "edit_file"])
def test_write_tools_map_to_write(name):
    """写类工具（category=file, 非只读）归为 write，而不是 command。

    这是 D1 的核心断言：修好前它们会落到 command 兜底分支。
    """
    t = get_default_registry().get(name)
    assert t.category() == "file"
    assert t.is_read_only() is False
    assert normalize_tool_category(t.category(), t.is_read_only()) == "write"


def test_bash_maps_to_command():
    """bash 仍是 command，不受本次修复影响。"""
    t = get_default_registry().get("bash")
    assert normalize_tool_category(t.category(), t.is_read_only()) == "command"


@pytest.mark.parametrize("name", ["glob", "grep"])
def test_search_tools_map_to_read(name):
    """检索类工具归为 read。"""
    t = get_default_registry().get(name)
    assert normalize_tool_category(t.category(), t.is_read_only()) == "read"


def test_plan_tool_maps_to_write():
    """ExitPlanMode 的 category 是 plan，映射为 write（既有行为，未改动）。"""
    t = get_default_registry().get("ExitPlanMode")
    assert normalize_tool_category(t.category(), t.is_read_only()) == "write"


def test_unknown_category_falls_back_to_read_or_command():
    """未识别的 category 退回兜底：只读→read，否则→command。"""
    assert normalize_tool_category("mcp", True) == "read"
    assert normalize_tool_category("mcp", False) == "command"
    assert normalize_tool_category("task", True) == "read"
    assert normalize_tool_category("task", False) == "command"


def test_default_registry_mapping_is_stable():
    """锁定内置工具的分类映射，防止再次漂移。"""
    reg = get_default_registry()
    expected = {
        "read_file": "read",
        "write_file": "write",
        "edit_file": "write",
        "bash": "command",
        "glob": "read",
        "grep": "read",
        "ExitPlanMode": "write",
    }
    actual = {
        name: normalize_tool_category(
            reg.get(name).category(), reg.get(name).is_read_only()
        )
        for name in expected
    }
    assert actual == expected


# ── 模式决策（D2：acceptEdits 必须与 default 可区分）────────────────


def _decide(mode: PermissionMode, tool_name: str, category: str, args: dict) -> str:
    checker = PermissionChecker(mode=mode)
    return checker.check(tool_name, False, category, args).effect


def test_accept_edits_allows_write_but_default_asks():
    """acceptEdits 对写文件放行，default 询问——两者必须可区分。

    修 D1 之前二者对 write_file 都返回 ask，acceptEdits 形同虚设。
    """
    args = {"file_path": "notes.txt", "content": "hi"}
    assert _decide(PermissionMode.ACCEPT_EDITS, "write_file", "write", args) == "allow"
    assert _decide(PermissionMode.DEFAULT, "write_file", "write", args) == "ask"


def test_accept_edits_still_asks_for_non_safe_command():
    """acceptEdits 只接受编辑，不放行非安全命令。"""
    args = {"command": "python build.py"}
    assert _decide(PermissionMode.ACCEPT_EDITS, "bash", "command", args) == "ask"


def test_dangerous_command_denied_in_every_mode():
    """危险命令黑名单在模式兜底之前生效，任何模式都不能绕过。"""
    args = {"command": "rm -rf /tmp/x"}
    for mode in PermissionMode:
        assert _decide(mode, "bash", "command", args) == "deny", mode


def test_safe_read_only_command_allowed_in_every_mode():
    """安全只读命令在 Layer 1 就放行，与模式无关。"""
    args = {"command": "git status"}
    for mode in PermissionMode:
        assert _decide(mode, "bash", "command", args) == "allow", mode


def test_bypass_allows_non_safe_command():
    """bypassPermissions 放行非安全命令（但危险命令仍被 Layer 1b 拦住，见上）。"""
    args = {"command": "python build.py"}
    assert _decide(PermissionMode.BYPASS, "bash", "command", args) == "allow"


# ── HITL 时序：yield 之后立刻 resolve 不得挂死 ──────────────────────────
#
# 回归背景：核心循环里 `_hitl_event.clear()` 曾位于 `yield HITLRequired(...)`
# **之后**。yield 把控制权交给调用方，而无人值守调用方（host._deny_hitl）
# 会在同一轮事件循环里立刻 resolve_hitl → set()；生成器恢复后随即 clear()，
# 把这个信号清掉，接着 wait() 永久阻塞。表现是「回合静默挂死：无事件、无日志、
# 无出站连接」。TUI 路径有人工操作延迟，set 总落在 wait() 之后，因此长期掩盖。
#
# 下面用 monkeypatch 把权限判定固定为 ask，让测试只针对时序本身，不与
# 具体权限策略（仍在演进）耦合。


def _hitl_agent():
    import tempfile
    from pathlib import Path

    from conversation.manager import ConversationManager
    from core.agent.agent import Agent
    from core.agent.config import AgentConfig
    from core.tool.context import ExecutionContext
    from core.tool.registry import ToolRegistry

    class _Cfg:
        model = "x"
        context_window = 200000

    class _Client:
        config = _Cfg()

    return Agent(
        registry=ToolRegistry(),
        llm_client=_Client(),
        exec_ctx=ExecutionContext(cwd=Path(tempfile.mkdtemp()), session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
    )


def _ask_tool_use(tu_id: str):
    from llm.stream_events import ToolUse

    return ToolUse(id=tu_id, name="write_file", input={"path": "a.txt", "content": "x"})


async def test_hitl_resolved_immediately_does_not_deadlock(monkeypatch):
    """无人值守消费模式：yield 后**立刻** resolve，回合必须正常收敛。

    这就是 host 的路径（自动拒绝、零人工延迟），修复前会永久卡住。
    """
    import asyncio

    from core.agent.events import HITLRequired
    from core.permissions.checker import Decision

    agent = _hitl_agent()
    monkeypatch.setattr(
        agent,
        "_check_tool_permission",
        lambda tu: Decision(effect="ask", reason="test-forces-ask"),
    )

    seen: list[str] = []

    async def consume():
        async for ev in agent._execute_tools([_ask_tool_use("tu-immediate")]):
            if isinstance(ev, HITLRequired):
                seen.append(ev.tool_use_id)
                agent.resolve_hitl(ev.tool_use_id, False, "deny")

    await asyncio.wait_for(consume(), timeout=5)
    assert seen == ["tu-immediate"]


async def test_hitl_resolved_from_another_task_works(monkeypatch):
    """TUI 式异步确认（下一轮事件循环再 resolve）同样不能被破坏。"""
    import asyncio

    from core.agent.events import HITLRequired
    from core.permissions.checker import Decision

    agent = _hitl_agent()
    monkeypatch.setattr(
        agent,
        "_check_tool_permission",
        lambda tu: Decision(effect="ask", reason="test-forces-ask"),
    )

    seen: list[str] = []

    async def consume():
        async for ev in agent._execute_tools([_ask_tool_use("tu-async")]):
            if isinstance(ev, HITLRequired):
                seen.append(ev.tool_use_id)
                asyncio.get_running_loop().call_soon(
                    agent.resolve_hitl, ev.tool_use_id, False, "deny"
                )

    await asyncio.wait_for(consume(), timeout=5)
    assert seen == ["tu-async"]


# ── 无人值守策略（host / 无头）─────────────────────────────────────────
#
# 无人环境没有按键的人，`ask` 级决策必须由策略代答，否则回合挂在 HITL 上
# 就是挂死（见文件上方 HITL 时序段）。这里固定两条语义：
#   1) 策略**只代答 ask**，不覆盖既有管线的 allow/deny；
#   2) 任何策略下都不得残留 ask，且交互式工具一律拒绝。


def _policy_agent(policy=None):
    """带默认注册表的 agent —— 策略判定依赖工具真实 category。"""
    import tempfile
    from pathlib import Path

    from conversation.manager import ConversationManager
    from core.agent.agent import Agent
    from core.agent.config import AgentConfig
    from core.tool.context import ExecutionContext

    class _Cfg:
        model = "x"
        context_window = 200000

    class _Client:
        config = _Cfg()

    agent = Agent(
        registry=get_default_registry(),
        llm_client=_Client(),
        exec_ctx=ExecutionContext(cwd=Path(tempfile.mkdtemp()), session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
    )
    if policy is not None:
        agent.set_unattended_policy(policy)
    return agent


def _policy_effect(agent, name: str, **tool_input: object) -> str:
    """走完整 `_check_tool_permission`（含无人值守代答）后的最终判定。"""
    from llm.stream_events import ToolUse

    return agent._check_tool_permission(
        ToolUse(id="tu", name=name, input=dict(tool_input))
    ).effect


ALL_POLICIES = ["allow_all", "allow_write", "deny_all"]

# host 默认档的判定矩阵（与实现同源，改行为必须同步改这里）
_CASES = [
    ("write_file", {"path": "a.txt", "content": "x"}, "write"),
    ("edit_file", {"path": "a.txt", "old": "a", "new": "b"}, "write"),
    ("read_file", {"path": "a.txt"}, "read"),
]


def test_allow_write_permits_write_and_read():
    agent = _policy_agent("allow_write")
    for name, inp, _ in _CASES:
        assert _policy_effect(agent, name, **inp) == "allow", name


def test_deny_all_denies_write_but_read_still_allowed():
    """默认档：ask 一律拒。只读工具本就不询问，不受策略影响。"""
    agent = _policy_agent("deny_all")
    assert _policy_effect(agent, "write_file", path="a.txt", content="x") == "deny"
    assert _policy_effect(agent, "edit_file", path="a.txt", old="a", new="b") == "deny"
    # 只读工具本来就走 allow（不是 ask），策略不该把它拦下来
    assert _policy_effect(agent, "read_file", path="a.txt") == "allow"


def test_allow_readonly_is_not_a_policy():
    """刻意不提供 allow_readonly：只读工具从不出现在 ask 里，该档等于 deny_all。

    这条断言把「不要加这个冗余档」钉住，避免后人"补全"它。
    """
    from core.permissions.modes import UnattendedPolicy

    assert not hasattr(UnattendedPolicy, "ALLOW_READONLY")
    with pytest.raises(ValueError):
        _policy_agent().set_unattended_policy("allow_readonly")


@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_no_ask_escape_under_any_policy(policy):
    """配了策略就不得残留 ask —— 无人环境里 ask 等于挂死。"""
    agent = _policy_agent(policy)
    for name, inp, _ in _CASES:
        assert _policy_effect(agent, name, **inp) != "ask", f"{policy}/{name}"
    assert _policy_effect(agent, "bash", command="ls") != "ask"


@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_interactive_tools_denied_under_every_policy(policy):
    """交互式工具在无人值守下一律拒绝（放行等于替人签字，且拿不到答案）。"""
    agent = _policy_agent(policy)
    assert _policy_effect(agent, "ExitPlanMode") == "deny"


def test_no_policy_keeps_ask_semantics():
    """不给策略 = 有人值守：写操作仍走 HITL，行为与改动前一致。"""
    agent = _policy_agent(None)
    assert _policy_effect(agent, "write_file", path="a.txt", content="x") == "ask"


def test_policy_accepts_str_and_enum():
    from core.permissions.modes import UnattendedPolicy

    a = _policy_agent()
    a.set_unattended_policy("allow_write")
    assert a.unattended_policy is UnattendedPolicy.ALLOW_WRITE
    a.set_unattended_policy(UnattendedPolicy.DENY_ALL)
    assert a.unattended_policy is UnattendedPolicy.DENY_ALL
    a.set_unattended_policy(None)
    assert a.unattended_policy is None


def test_invalid_policy_raises():
    agent = _policy_agent()
    with pytest.raises(ValueError):
        agent.set_unattended_policy("allow_everything")


@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_dangerous_command_denied_under_every_policy(policy):
    """危险命令在任何无人值守档位下都必须 deny。

    策略是"代答 ask"的机制，不能成为绕过危险命令黑名单的后门——
    尤其 `allow_all` 这一档最容易被误读成"什么都能跑"。
    无策略（有人值守）时同样是 deny，见既有 `test_dangerous_command_denied_in_every_mode`。
    """
    agent = _policy_agent(policy)
    assert _policy_effect(agent, "bash", command="rm -rf /tmp/somewhere") == "deny"


def test_allow_write_denies_non_safe_command():
    """`allow_write` 放的是"读+写"，命令执行不在其中。

    安全只读命令（ls）由既有规则引擎直接放行，与策略无关；
    非安全命令走 ask → 被策略代答为 deny。
    """
    agent = _policy_agent("allow_write")
    assert _policy_effect(agent, "bash", command="curl http://example.com") == "deny"
    assert _policy_effect(agent, "bash", command="ls -la") == "allow"


# ── 子 Agent 策略继承的推导规则（D4）──────────────────────────────────
#
# 子 Agent 没有 TUI 接 HITL，必须由策略代答 ask 才不会挂起；但授权范围
# 不得大于父。这两条断言把规则钉住。


def test_child_policy_prefers_parent_explicit_policy():
    """父显式设过策略（host 场景）→ 原样继承，不被父的 permission_mode 拉偏。"""
    from core.permissions.modes import UnattendedPolicy, resolve_child_policy

    class _Parent:
        unattended_policy = UnattendedPolicy.ALLOW_WRITE
        permission_mode = PermissionMode.BYPASS  # 故意不一致：显式策略优先

    assert resolve_child_policy(_Parent()) is UnattendedPolicy.ALLOW_WRITE


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (PermissionMode.BYPASS, "allow_all"),
        (PermissionMode.ACCEPT_EDITS, "allow_write"),
        # DEFAULT：父自己写文件也要经人批准 → 说明"写"是用户认可的意图，
        # 子 Agent 无人可问，取 allow_write（命令仍拒），比早先无条件 BYPASS 收敛
        (PermissionMode.DEFAULT, "allow_write"),
        (PermissionMode.PLAN, "deny_all"),
    ],
)
def test_child_policy_derived_from_parent_mode(mode, expected):
    """父未设策略时按父的权限模式推导 —— 授权对齐，不多不少。"""
    from core.permissions.modes import resolve_child_policy

    class _Parent:
        unattended_policy = None
        permission_mode = mode

    assert resolve_child_policy(_Parent()).value == expected


def test_child_policy_falls_back_to_deny_all_on_unknown_parent():
    """取不到父的模式时退回最保守档（宁可少授权）。"""
    from core.permissions.modes import resolve_child_policy

    class _Parent:
        pass

    assert resolve_child_policy(_Parent()).value == "deny_all"
