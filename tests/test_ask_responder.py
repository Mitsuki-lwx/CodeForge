"""审批应答机制的收口与安全网测试。

对应 `docs/spec_subagent.md` 附录 B（Skill fork 挂死 → 收口 + 安全网）。
覆盖三块：

1. `arming_approval` 的分支与还原
2. `_ensure_ask_responder` 安全网
3. 真实 `_execute_tools` 上的端到端不卡（含"拒绝原因可读"）
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.sub_agent import arming_approval, run_to_completion
from core.permissions.hitl import HITLChoice, HITLResponse
from core.permissions.modes import PermissionMode, UnattendedPolicy
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


class _Parent:
    """主 Agent 替身：`resolve_child_policy` 只读这两项。"""

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT) -> None:
        self.permission_mode = mode
        self.unattended_policy = None


def _mk_agent(cwd: Path, *, upgrader=None, name: str = "calc") -> Agent:
    agent = Agent(
        registry=get_default_registry(),
        llm_client=_Client(),
        exec_ctx=ExecutionContext(
            cwd=cwd, session_id="sub", approval_upgrader=upgrader
        ),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
        permission_mode=PermissionMode.DEFAULT,
    )
    agent.set_agent_name(name)
    return agent


def _serving_upgrader(seen: list) -> ApprovalUpgrader:
    up = ApprovalUpgrader()
    up.start(
        lambda req: (
            seen.append(req),
            HITLResponse(choice=HITLChoice.ALLOW_ONCE, tool_name=req.tool_name),
        )[1]
    )
    return up


async def _drain(agent: Agent, calls: list[ToolUse]) -> None:
    async for _ev in agent._execute_tools(calls):
        pass


def _result_of(agent: Agent, call_id: str) -> str:
    for m in reversed(agent._conversation.messages):
        if getattr(m, "tool_use_id", None) == call_id:
            return str(getattr(m, "content", ""))
    return ""


# ── 1. arming_approval 的分支 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_branch1_channel_armed_and_policy_stripped(tmp_path):
    """有通道、角色未 `dontask` → 挂通道，并把策略摘掉（否则通道轮不到）。"""
    up = _serving_upgrader([])
    agent = _mk_agent(tmp_path, upgrader=up)
    agent.set_unattended_policy(UnattendedPolicy.ALLOW_WRITE)
    try:
        with arming_approval(agent, parent=_Parent()):
            assert agent._approval_upgrader is up, "通道必须被挂上"
            assert agent.unattended_policy is None, "策略必须被摘掉"
        await asyncio.sleep(0)
    finally:
        await up.stop()
    assert agent.unattended_policy is UnattendedPolicy.ALLOW_WRITE, "退出要还原"
    assert agent._approval_upgrader is None, "退出要摘掉通道"


@pytest.mark.asyncio
async def test_branch1_skipped_when_role_declared_dontask(tmp_path):
    """角色**显式** `dontask` → 不抢它的通道（尊重角色契约）。

    能力清单第 9 条的层次是『账本 → 角色 permission_mode 兜底（含 dontAsk）→
    升级到主 TUI』，`dontAsk` 是中间层、先于升级。
    """
    up = _serving_upgrader([])
    agent = _mk_agent(tmp_path, upgrader=up)
    agent._dont_ask = True
    try:
        with arming_approval(agent, parent=_Parent()):
            assert agent._approval_upgrader is None, "不该抢 dontask 角色的通道"
            assert agent._dont_ask is True
        await asyncio.sleep(0)
    finally:
        await up.stop()


@pytest.mark.asyncio
async def test_branch2_falls_back_by_parent_scope(tmp_path):
    """三种应答机制全无 → 按父的授权范围兜底（DEFAULT → allow_write）。"""
    agent = _mk_agent(tmp_path)
    with arming_approval(agent, parent=_Parent(PermissionMode.DEFAULT)):
        assert agent.unattended_policy is UnattendedPolicy.ALLOW_WRITE
    assert agent.unattended_policy is None, "退出要还原"


@pytest.mark.asyncio
async def test_branch2_without_parent_is_most_conservative(tmp_path):
    """拿不到父 → 取最保守档，绝不静默挂死。"""
    agent = _mk_agent(tmp_path)
    with arming_approval(agent, parent=None):
        assert agent.unattended_policy is UnattendedPolicy.DENY_ALL
    assert agent.unattended_policy is None


@pytest.mark.asyncio
async def test_branch3_existing_responder_untouched(tmp_path):
    """已有应答者（策略）→ 什么都不做、一个属性都不碰。"""
    agent = _mk_agent(tmp_path)
    agent.set_unattended_policy(UnattendedPolicy.ALLOW_WRITE)
    before = agent.unattended_policy
    with arming_approval(agent, parent=_Parent()):
        assert agent.unattended_policy is before
        assert agent._approval_upgrader is None
    assert agent.unattended_policy is before


@pytest.mark.asyncio
async def test_arming_is_idempotent_and_restores_on_exception(tmp_path):
    """异常逃出也要还原（否则会污染被续派复用的实例）。"""
    agent = _mk_agent(tmp_path)
    agent.set_unattended_policy(UnattendedPolicy.ALLOW_WRITE)
    # 嵌套 `with` 是**故意的**（本用例就是在测重入），不能合并成
    # `with A, B:`——那会变成顺序进入同一层，测不出嵌套。
    with pytest.raises(RuntimeError):  # noqa: SIM117
        with arming_approval(agent, parent=_Parent()):
            raise RuntimeError("boom")
    assert agent.unattended_policy is UnattendedPolicy.ALLOW_WRITE

    # 重复进出不抛
    with arming_approval(agent, parent=_Parent()):  # noqa: SIM117
        with arming_approval(agent, parent=_Parent()):
            pass
    assert agent.unattended_policy is UnattendedPolicy.ALLOW_WRITE


def test_arming_tolerates_non_agent_stub():
    """单测里常见的 `object()` 桩：没有可接入的机制 → 原样透传，不抛。"""

    class _Stub:  # 故意不给 set_unattended_policy
        pass

    with arming_approval(_Stub(), parent=None):
        pass


# ── 2. 安全网 ────────────────────────────────────────────────────────


def test_guard_sets_deny_all_and_warns(tmp_path, caplog):
    """三种全无 → 记醒目 warning + 最保守档兜底。"""
    from core.agent.sub_agent import _ensure_ask_responder

    agent = _mk_agent(tmp_path, name="orphan")
    with caplog.at_level(logging.WARNING):
        _ensure_ask_responder(agent)

    assert agent.unattended_policy is UnattendedPolicy.DENY_ALL
    assert any("无人应答" in r.message or "全无" in r.message for r in caplog.records)
    assert any("orphan" in r.getMessage() for r in caplog.records), "告警要能指明是谁"


def test_guard_does_not_touch_agent_with_responder(tmp_path, caplog):
    """有应答者 → 不触发、不写任何属性。"""
    from core.agent.sub_agent import _ensure_ask_responder

    agent = _mk_agent(tmp_path)
    agent.set_unattended_policy(UnattendedPolicy.ALLOW_WRITE)
    with caplog.at_level(logging.WARNING):
        _ensure_ask_responder(agent)
    assert agent.unattended_policy is UnattendedPolicy.ALLOW_WRITE
    assert not [r for r in caplog.records if "应答" in r.getMessage()]


@pytest.mark.asyncio
async def test_run_to_completion_does_not_hang_on_bare_agent(tmp_path):
    """裸 Agent（三种全无）跑到底**不再挂死** —— 安全网在 `run_to_completion` 入口拦下。"""

    class _ClientToolThenText:
        config = _Cfg()

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(
            self, messages, system_prompt="", tools=None, system_blocks=None
        ):
            from llm.stream_events import CompletionDone, TextChunk, ToolUse

            self.calls += 1
            if self.calls == 1:
                yield ToolUse(
                    id="c1", name="bash", input={"command": "pytest -q"}
                )
            else:
                yield TextChunk(text="done")
            yield CompletionDone(usage={"input_tokens": 1, "output_tokens": 1})

    agent = _mk_agent(tmp_path)
    agent._client = _ClientToolThenText()

    text = await asyncio.wait_for(
        run_to_completion(agent, agent._conversation, task="跑测试"), timeout=10
    )
    assert "cancelled" not in str(text).lower()
    assert text  # 收尾了


# ── 3. 真实 `_execute_tools`：不卡 + 原因可读 ────────────────────────


@pytest.mark.asyncio
async def test_policy_denied_command_has_readable_reason(tmp_path):
    """被策略拒的命令，子 Agent 拿到的原因必须可读（不是静默、不是空串）。"""
    agent = _mk_agent(tmp_path)
    agent.set_unattended_policy(UnattendedPolicy.ALLOW_WRITE)  # command 仍拒

    await asyncio.wait_for(
        _drain(agent, [ToolUse(id="t1", name="bash", input={"command": "pytest -q"})]),
        timeout=10,
    )

    reason = _result_of(agent, "t1")
    assert "unattended:allow_write" in reason, f"原因应含策略档位，实际={reason!r}"
    assert "command" in reason


@pytest.mark.asyncio
async def test_armed_agent_escalates_to_channel(tmp_path):
    """被 `arming_approval` 装过的 Agent：ask 真的冒泡到通道，不再"无人应答"。

    这是 Skill fork 那条路径的核心断言 —— 未装之前请求会永远停在
    `await _hitl_event.wait()` 上；装完之后它出现在通道里。
    """
    seen: list = []
    up = _serving_upgrader(seen)
    agent = _mk_agent(tmp_path, upgrader=up, name="forkx")
    agent.set_unattended_policy(UnattendedPolicy.ALLOW_WRITE)  # 不装就会被它代答

    try:
        # 写文件在 DEFAULT 下是 `ask`；策略被摘掉后才轮得到通道
        with arming_approval(agent, parent=_Parent()):
            await asyncio.wait_for(
                _drain(
                    agent,
                    [
                        ToolUse(
                            id="t1",
                            name="write_file",
                            input={"file_path": "x.txt", "content": "hi"},
                        )
                    ],
                ),
                timeout=10,
            )
    finally:
        await up.stop()

    assert len(seen) == 1, "请求必须冒泡到通道（否则就是又绕回了策略代答）"
    assert seen[0].origin == "forkx", "冒泡的请求要带来源身份"
    assert (tmp_path / "x.txt").exists(), "界面允许后工具应执行"
