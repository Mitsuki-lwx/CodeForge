"""子 Agent 的**会话同源**不变量测试。

对应 `docs/spec_subagent_empty_output.md`：
子 Agent 曾经有**两个** `ConversationManager` —— 助手消息进一个、工具结果进另一个，
于是模型**永远看不到工具输出**，只会反复重发同一批调用，跑满 `max_turns` 后返回空字符串。

三个入口都中招（Agent 工具 / pane 队友 / 后台任务），只有 skill fork 恰好传对了对象。

这里钉住三条：
1. 传了"外来会话"时，`run_to_completion` 会**收口**到 agent 自己的会话（并告警）
2. **同一轮里**助手消息与工具结果落在同一个会话上（第二轮能看到第一轮的工具结果）
3. **真实入口**（`AgentTool.execute`）传给 `run_to_completion` 的，就是子 Agent 自己的会话
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.sub_agent import run_to_completion
from core.permissions.modes import PermissionMode
from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from llm.stream_events import CompletionDone, TextChunk, ToolUse


class _Cfg:
    protocol = "openai"
    model = "scripted"
    base_url = None
    vendor = None
    context_window = 200000
    thinking = False


class _ScriptedClient:
    """按脚本逐轮产出流式事件，并记录**每轮实际喂进去的 messages**。

    记录 messages 是关键：断言"第二轮能看到第一轮的工具结果"，
    直接证明工具结果与助手消息落在同一个会话上。
    """

    def __init__(self, script: list[tuple[str, list[ToolUse]]]) -> None:
        self.config = _Cfg()
        self._script = script
        self.turns = 0
        self.seen: list[list[Any]] = []

    async def stream_chat(self, messages: list[Any], **_kwargs: Any):
        self.seen.append(messages)
        idx = self.turns
        self.turns += 1
        text, tools = self._script[idx] if idx < len(self._script) else ("", [])
        if text:
            yield TextChunk(text=text)
        for tu in tools:
            yield tu
        yield CompletionDone(usage=None)


def _mk_agent(cwd: Path, client: Any) -> Agent:
    return Agent(
        registry=get_default_registry(),
        llm_client=client,
        exec_ctx=ExecutionContext(cwd=cwd, session_id="sub"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=4),
        permission_mode=PermissionMode.DEFAULT,
    )


def _content_blocks(m: Any) -> list[Any]:
    """消息正文若是块数组就取出来（工具调用/结果都是块，不是纯文本）。"""
    content = getattr(m, "content", None)
    return content if isinstance(content, list) else []


def _has_block(m: Any, block_type: str) -> bool:
    return any(
        isinstance(b, dict) and b.get("type") == block_type for b in _content_blocks(m)
    )


def _tool_result_count(messages: list[Any]) -> int:
    """数一遍喂进去的消息里有多少条工具结果。

    工具结果在这里是 `role=user` + `[{"type": "tool_result", ...}]` 块数组，
    所以不能按 role 判断，要看块类型。
    """
    return sum(1 for m in messages if _has_block(m, "tool_result"))


def _api_messages(conv: ConversationManager) -> list[Any]:
    return conv.to_api_format()[0]


# ── 1. 收口：外来会话会被纠偏 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_conversation_is_corrected(tmp_path, caplog):
    """传入与 `agent._conversation` 不同的会话 → 收口到 agent 自己的，并告警。

    这正是修前的线上形态：调用方另建一个 conv 传进来，于是分裂。
    """
    client = _ScriptedClient([("done", [])])
    agent = _mk_agent(tmp_path, client)
    foreign = ConversationManager()

    with caplog.at_level(logging.WARNING):
        text = await run_to_completion(agent, foreign, "hi")

    assert text == "done"
    assert any("不是同一个对象" in r.message for r in caplog.records), "必须留下告警"
    # 消息必须落在 agent 自己的会话上，**不能**落在那个外来会话里
    assert len(_api_messages(agent._conversation)) >= 2
    assert _api_messages(foreign) == [], "外来会话不该被写进任何东西"


# ── 2. 核心不变量：工具结果与助手消息同源 ──────────────────────────


@pytest.mark.asyncio
async def test_tool_result_visible_to_next_turn(tmp_path):
    """**第二轮必须能看到第一轮的工具结果** —— 这条不成立就是"空产出"的根因。"""
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")

    tu = ToolUse(id="t1", name="read_file", input={"file_path": str(target)})
    client = _ScriptedClient([("", [tu]), ("看完了", [])])
    agent = _mk_agent(tmp_path, client)

    text = await run_to_completion(agent, agent._conversation, "读一下文件")

    assert client.turns == 2, f"应当跑两轮（工具轮 + 收尾轮），实际 {client.turns}"
    assert _tool_result_count(client.seen[1]) >= 1, (
        "第二轮喂进去的消息里必须有工具结果 —— 否则模型看不到工具输出，"
        "只会反复重发同一批调用，最终产出空内容"
    )
    assert text == "看完了", "最终应当产出非空文本"


@pytest.mark.asyncio
async def test_assistant_and_tool_share_one_conversation(tmp_path):
    """助手消息（含 tool_calls）与工具结果必须落在**同一个**会话里。"""
    target = tmp_path / "a.txt"
    target.write_text("x\n", encoding="utf-8")
    tu = ToolUse(id="t1", name="read_file", input={"file_path": str(target)})
    client = _ScriptedClient([("", [tu]), ("好了", [])])
    agent = _mk_agent(tmp_path, client)

    await run_to_completion(agent, agent._conversation, "读文件")

    msgs = _api_messages(agent._conversation)
    assert any(_has_block(m, "tool_use") for m in msgs), "助手消息（含 tool_use）应当在会话里"
    assert any(_has_block(m, "tool_result") for m in msgs), (
        "工具结果应当在**同一个**会话里"
    )


# ── 3. 真实入口：execute() 传的就是子 Agent 自己的会话 ───────────────


class _Cfg2:
    model = "x"
    context_window = 200000


class _Client2:
    config = _Cfg2()


@pytest.mark.asyncio
async def test_agent_tool_entry_passes_agents_own_conversation(tmp_path, monkeypatch):
    """**走真实入口**：`AgentTool.execute()` 必须把子 Agent 自己的会话传下去。

    这条是修前漏掉的那个盲区 —— 单测自己造 agent + conv 传进去，
    从不经过 `execute()` → `_build_sub_agent()` → `run_to_completion()` 这条配对路径。
    """
    from core.agent.role_loader import load_catalog
    from core.tool.tools.agent_tool import AgentTool

    captured: dict[str, Any] = {}

    async def _fake_rtc(agent, conv, task="", events=None):
        captured["agent"] = agent
        captured["conv"] = conv
        return "ok"

    monkeypatch.setattr("core.agent.sub_agent.run_to_completion", _fake_rtc)

    parent = Agent(
        registry=get_default_registry(),
        llm_client=_Client2(),
        exec_ctx=ExecutionContext(cwd=tmp_path, session_id="main"),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
    )
    catalog = load_catalog(str(tmp_path))
    tool = AgentTool(catalog=catalog, task_mgr=None, bg_enabled=True)
    tool.set_parent(parent)

    ctx = ExecutionContext(cwd=tmp_path, session_id="main")
    await tool.execute(ctx, {"prompt": "统计行数", "description": "d", "subagent_type": "Explore"})

    assert "conv" in captured, "run_to_completion 应当被调用"
    assert captured["conv"] is captured["agent"]._conversation, (
        "传给 run_to_completion 的会话必须是子 Agent 自己的那个 —— "
        "否则工具结果与助手消息会分裂，回合产出空内容"
    )
