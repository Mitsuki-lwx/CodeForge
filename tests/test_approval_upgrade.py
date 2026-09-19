"""审批升级链测试 —— 子 Agent 的 `ask` 冒泡到上层通道。

对应 `docs/spec_subagent.md` 附录 A（能力清单第 9 条的第三层）。
本文件覆盖三块：

1. `ApprovalUpgrader` 自身的契约（fail-closed / 超时 / 消费者异常 / 启停）
2. 来源身份（`origin`）在数据模型与弹窗上的透传
3. `Agent._execute_tools` 的 `ask` 分流：有通道走通道、无通道保持原路径
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.events import HITLRequired
from core.permissions.hitl import HITLChoice, HITLRequest, HITLResponse
from core.permissions.modes import PermissionMode
from core.permissions.upgrade import ApprovalUpgrader
from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from llm.stream_events import ToolUse

# ── 构造 ────────────────────────────────────────────────────────────


class _Cfg:
    protocol = "anthropic"
    model = "test"
    context_window = 200000


class _Client:
    config = _Cfg()


def _mk_agent(cwd: Path, name: str = "sub") -> Agent:
    """裸子 Agent：DEFAULT 模式、不设无人值守策略（`ask` 保持 `ask`）。"""
    agent = Agent(
        registry=get_default_registry(),
        llm_client=_Client(),
        exec_ctx=ExecutionContext(cwd=cwd, session_id="sub"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
        permission_mode=PermissionMode.DEFAULT,
    )
    agent.set_agent_name(name)
    return agent


def _write_call(path: str = "a.txt", call_id: str = "t1") -> ToolUse:
    return ToolUse(id=call_id, name="write_file", input={"file_path": path, "content": "x"})


def _allow_handler(seen: list[HITLRequest], choice=HITLChoice.ALLOW_ONCE):
    def handler(req: HITLRequest) -> HITLResponse:
        seen.append(req)
        return HITLResponse(choice=choice, tool_name=req.tool_name)

    return handler


async def _drain(agent: Agent, calls: list[ToolUse]) -> None:
    async for _ev in agent._execute_tools(calls):
        pass


# ── 1. ApprovalUpgrader 契约 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_consumer_is_fail_closed_not_hang(tmp_path):
    """没消费者 → 立即拒绝，**不挂起**。这是通道路径最要紧的一条。"""
    up = ApprovalUpgrader()
    assert up.serving is False

    outcome = await asyncio.wait_for(
        up.request(HITLRequest(tool_name="write_file", description="")), timeout=2
    )
    assert outcome.allowed is False
    assert "无人可问" in outcome.reason


@pytest.mark.asyncio
async def test_serving_flips_true_synchronously_on_start():
    """`start()` 必须**同步**置位 serving。

    回归锁：`create_task` 只是把协程排进就绪队列，任务跑第一步之前 serving 仍是
    False。请求若在这中间到达会被误判成"无人可问" —— 实测踩到过。
    """
    up = ApprovalUpgrader()
    up.start(lambda req: HITLResponse(choice=HITLChoice.ALLOW_ONCE, tool_name="x"))
    try:
        assert up.serving is True, "start() 后必须立刻可服务，不能等任务被调度"
    finally:
        await up.stop()


@pytest.mark.asyncio
async def test_request_is_answered_by_consumer():
    up = ApprovalUpgrader()
    seen: list[HITLRequest] = []
    up.start(_allow_handler(seen))
    try:
        outcome = await asyncio.wait_for(
            up.request(HITLRequest(tool_name="write_file", description="")), timeout=5
        )
    finally:
        await up.stop()

    assert outcome.allowed is True
    assert outcome.choice == HITLChoice.ALLOW_ONCE.value
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_deny_choice_yields_readable_reason():
    up = ApprovalUpgrader()
    up.start(_allow_handler([], choice=HITLChoice.DENY))
    try:
        outcome = await asyncio.wait_for(
            up.request(HITLRequest(tool_name="write_file", description="")), timeout=5
        )
    finally:
        await up.stop()

    assert outcome.allowed is False
    assert "拒绝" in outcome.reason


@pytest.mark.asyncio
async def test_timeout_is_fail_closed_with_reason():
    """超时 → 拒绝，且原因写明超时（不能静默、不能挂死）。"""
    up = ApprovalUpgrader(timeout=0.05)

    def slow(_req: HITLRequest) -> HITLResponse:
        time.sleep(0.4)
        return HITLResponse(choice=HITLChoice.ALLOW_ONCE, tool_name="x")

    up.start(slow)
    try:
        outcome = await asyncio.wait_for(
            up.request(HITLRequest(tool_name="write_file", description="")), timeout=5
        )
    finally:
        await up.stop()

    assert outcome.allowed is False
    assert "超时" in outcome.reason


@pytest.mark.asyncio
async def test_consumer_exception_is_contained():
    """消费者抛异常 → 该请求被拒，不把回合掀掉。"""
    up = ApprovalUpgrader()

    def boom(_req: HITLRequest) -> HITLResponse:
        raise RuntimeError("dialog crashed")

    up.start(boom)
    try:
        outcome = await asyncio.wait_for(
            up.request(HITLRequest(tool_name="write_file", description="")), timeout=5
        )
    finally:
        await up.stop()

    assert outcome.allowed is False
    assert "失败" in outcome.reason


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_clears_serving():
    up = ApprovalUpgrader()
    up.start(_allow_handler([]))
    await up.stop()
    assert up.serving is False
    await up.stop()  # 二次调用不得抛
    assert up.serving is False


@pytest.mark.asyncio
async def test_start_is_idempotent():
    up = ApprovalUpgrader()
    up.start(_allow_handler([]))
    first = up._task
    up.start(_allow_handler([]))
    try:
        assert up._task is first, "重复 start 不该起第二个消费者"
    finally:
        await up.stop()


# ── 2. 来源身份 ─────────────────────────────────────────────────────


def test_hitl_request_origin_defaults_to_empty():
    """默认空串 = 主 Agent 自己发起 —— 既有调用方不传也不该出问题。"""
    assert HITLRequest(tool_name="w", description="").origin == ""


def test_hitl_required_origin_defaults_to_empty():
    ev = HITLRequired(tool_name="w", tool_use_id="t", description="", arguments={})
    assert ev.origin == ""


def test_dialog_shows_origin_only_when_present(monkeypatch):
    """弹窗标题带来源；主 Agent 的请求（origin 空）不该多出这一段。"""
    import tui.hitl_dialog as dlg

    captured: list[str] = []

    def fake_select(console, *, title, **kwargs):
        captured.append(title)
        return 0  # 第一项 = allow_once

    monkeypatch.setattr(dlg, "select_from_options", fake_select)

    class _Console:
        width = 80

    dlg.show_hitl_dialog(
        _Console(),
        HITLRequest(tool_name="write_file", description="d", origin="calc"),
    )
    dlg.show_hitl_dialog(_Console(), HITLRequest(tool_name="write_file", description="d"))

    assert "[来自 SubAgent calc]" in captured[0]
    assert "SubAgent" not in captured[1], "主 Agent 自己的请求不该标来源"


# ── 3. `_execute_tools` 的 ask 分流 ──────────────────────────────────


@pytest.mark.asyncio
async def test_ask_goes_through_channel_when_present(tmp_path):
    """挂了通道 → 走通道；请求带来源；放行后工具真的执行。"""
    agent = _mk_agent(tmp_path, name="calc")
    up = ApprovalUpgrader()
    agent._approval_upgrader = up
    seen: list[HITLRequest] = []
    up.start(_allow_handler(seen))

    try:
        await asyncio.wait_for(_drain(agent, [_write_call()]), timeout=10)
    finally:
        await up.stop()

    assert len(seen) == 1, "请求没有走到通道"
    assert seen[0].origin == "calc"
    assert (tmp_path / "a.txt").exists(), "放行后工具应当真的执行"


@pytest.mark.asyncio
async def test_ask_channel_deny_blocks_tool(tmp_path):
    agent = _mk_agent(tmp_path)
    up = ApprovalUpgrader()
    agent._approval_upgrader = up
    up.start(_allow_handler([], choice=HITLChoice.DENY))

    try:
        await asyncio.wait_for(_drain(agent, [_write_call()]), timeout=10)
    finally:
        await up.stop()

    assert not (tmp_path / "a.txt").exists(), "拒绝后工具不该执行"


@pytest.mark.asyncio
async def test_allow_session_is_recorded_on_own_checker(tmp_path):
    """「本会话允许」只记在**子 Agent 自己的**账本上：第二次同类调用不再问。

    记到主 Agent 的账本上会造成反向越权（为子 Agent 放行 = 给主 Agent 也放行）。
    """
    agent = _mk_agent(tmp_path)
    up = ApprovalUpgrader()
    agent._approval_upgrader = up
    seen: list[HITLRequest] = []
    up.start(_allow_handler(seen, choice=HITLChoice.ALLOW_SESSION))

    try:
        await asyncio.wait_for(
            _drain(agent, [_write_call("s.txt", "t1"), _write_call("s.txt", "t2")]),
            timeout=10,
        )
    finally:
        await up.stop()

    assert len(seen) == 1, "第二次同类调用应当命中会话账本，不再问"
    assert (tmp_path / "s.txt").exists()


@pytest.mark.asyncio
async def test_without_upgrader_still_yields_hitl_event(tmp_path):
    """没有通道时，`ask` 仍走原来的 `yield HITLRequired` —— 主 Agent 路径零回归。"""
    agent = _mk_agent(tmp_path)
    assert agent._approval_upgrader is None

    gen = agent._execute_tools([_write_call()])
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=5)
    finally:
        await gen.aclose()

    assert isinstance(first, HITLRequired)
    assert first.origin == "" or first.origin == "sub"


@pytest.mark.asyncio
async def test_upgrader_absent_on_main_agent_by_default(tmp_path):
    """主 Agent 默认不带通道 —— 否则它自己的 ask 会绕开 `yield HITLRequired`。"""
    agent = _mk_agent(tmp_path)
    assert agent._approval_upgrader is None
