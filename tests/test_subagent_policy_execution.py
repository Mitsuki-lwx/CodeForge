"""子 Agent 权限继承的**真实执行链**验证（D4）。

区别于 `test_agent_tool.py` 里"断言 `Decision.effect`"的用例：这里走完
`_execute_tools` 的真实入口 + 真实工具 + 真实文件系统，确认权限链**真的**
拦住了写操作（或真的放行），而不是只在决策层返回 deny 却照样执行。

成对出现：`deny_all` 不落地 / `allow_write` 真落地 —— 单看一边都可能被
"所有写都被拦"或"权限根本没生效"蒙混过去。

两个容易踩的点（实测踩过）：
  1. `write_file` 的参数名是 **`file_path`**，写成 `path` 会得到
     "Validation failed: 'file_path' is a required property"，
     看着像"写不进去"，其实是自己的用例写错了。
  2. 被 deny 的工具**不会产生任何 `ToolCall*` 事件** —— 它在权限预检阶段就被
     拦下、根本不进入执行批次。所以 deny 场景只能用"文件没落地"作判据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.runtime import SessionRuntime
from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from core.tool.tools.agent_tool import AgentTool
from llm.stream_events import ToolUse


class _MockClient:
    """够 Agent 构造用的最小 LLM 客户端（本测试不真的调模型）。"""

    class _Cfg:
        model = "x"
        context_window = 200000

    config = _Cfg()


def _parent(tmp_path: Path, policy: str | None) -> Agent:
    agent = Agent(
        registry=get_default_registry(),
        llm_client=_MockClient(),
        exec_ctx=ExecutionContext(cwd=tmp_path, session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
        runtime=SessionRuntime(),
    )
    agent.set_unattended_policy(policy)
    return agent


def _fork_child(parent: Agent) -> Agent:
    tool = AgentTool(catalog=None, task_mgr=None, bg_enabled=False)
    tool.set_parent(parent)
    return tool._build_sub_agent(
        role=None, allowed=["write_file"], session_id="main", is_fork=True
    )


async def _run_write(agent: Agent, path: str) -> list:
    """真实跑一次 write_file，返回 `ToolCallFinished` 事件（deny 时为空）。"""
    tool_use = ToolUse(
        id="tu1", name="write_file", input={"file_path": path, "content": "payload"}
    )
    finished: list = []
    async for ev in agent._execute_tools([tool_use]):
        if type(ev).__name__ == "ToolCallFinished":
            finished.append(ev)
    return finished


@pytest.mark.asyncio
async def test_deny_all_parent_blocks_child_write_on_disk(tmp_path):
    """父 `deny_all` → 子 Agent 的写操作**不落地**、且不进入执行批次。"""
    sub = _fork_child(_parent(tmp_path, "deny_all"))

    finished = await _run_write(sub, "blocked.txt")

    assert not (tmp_path / "blocked.txt").exists(), "被 deny 的写操作不得落地"
    assert finished == [], "被 deny 的工具不应进入执行批次"


@pytest.mark.asyncio
async def test_allow_write_parent_lets_child_write_on_disk(tmp_path):
    """对照组：父 `allow_write` → 子 Agent 的写操作**真落地**。

    没有这一条，"写不进去"也可能只是路径/参数问题被误读成"策略生效"。
    """
    sub = _fork_child(_parent(tmp_path, "allow_write"))

    finished = await _run_write(sub, "allowed.txt")

    target = tmp_path / "allowed.txt"
    assert target.exists(), "allow_write 档下子 Agent 应能写"
    assert target.read_text(encoding="utf-8") == "payload"
    assert finished and finished[0].success is True


@pytest.mark.asyncio
async def test_parent_without_policy_child_can_write_but_not_run_command(tmp_path):
    """父未设策略（有人值守 TUI）+ 默认模式 → 子能写文件，但**命令执行仍被拒**。

    这条同时说明"收敛"落在哪里：早先 fork 是无条件 `BYPASS`（连命令都放行），
    现在即便最宽的推导档也只是 `allow_write`。
    """
    sub = _fork_child(_parent(tmp_path, None))

    finished = await _run_write(sub, "child-can-write.txt")

    assert (tmp_path / "child-can-write.txt").exists()
    assert finished and finished[0].success is True

    # 同一个子 Agent：写放行，但非安全命令仍拒（allow_write 不含 command）
    decision = sub._check_tool_permission(
        ToolUse(id="tu2", name="bash", input={"command": "curl http://example.com"})
    )
    assert decision.effect == "deny"
