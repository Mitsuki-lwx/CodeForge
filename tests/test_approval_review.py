"""审批自动审查者测试（spec_approval_review）。

三层一起测，因为它们构成一条链：
  1. **解析层** —— `parse_review_output` 的容错（模型输出不可控）
  2. **审查者** —— `ApprovalReviewer.review` 的**五路失败必须 fail-closed**
  3. **接线** —— `REVIEW` 档在决策链里的行为，以及其余档位的零回归

核心纪律：审查者存在的意义是"没人看着时把住关"。**它自己出问题时必须萎缩成
最保守的那一档，绝不能变成放行通道。** 所以失败路径的断言比成功路径更重要。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from core.permissions.modes import UnattendedPolicy, unattended_decide
from core.permissions.reviewer import (
    REVIEW_POLICY,
    ApprovalReviewer,
    ReviewContext,
    build_review_input,
    parse_review_output,
)
from llm.stream_events import CompletionDone, StreamError, TextChunk

# ── 可编程的假 client ────────────────────────────────────────────────


class _FakeClient:
    """按脚本产出流式事件，用来精确制造各条失败路径。

    只实现 `stream_chat`——审查者只调它，这也顺带证明了审查者**不需要别的能力**。
    """

    def __init__(self, *, text: str | None = None, events=None, raise_exc=None, delay=0.0):
        self._text = text
        self._events = events
        self._raise = raise_exc
        self._delay = delay

    async def stream_chat(self, messages, system_prompt="", tools=None, system_blocks=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        if self._events is not None:
            for ev in self._events:
                yield ev
            return
        yield TextChunk(text=self._text or "")
        yield CompletionDone()


def _reviewer(client, timeout: float = 5.0) -> ApprovalReviewer:
    return ApprovalReviewer(client, timeout=timeout)


def _ctx() -> ReviewContext:
    return ReviewContext(
        tool_name="write_file",
        tool_input={"path": "a.txt", "content": "x"},
        category="write",
        permission_reason="需要用户确认",
        cwd="/tmp/proj",
        user_intent=["帮我改一下 a.txt"],
    )


async def _review(client, timeout: float = 5.0):
    return await _reviewer(client, timeout).review(_ctx())


# ── 1. 解析层 ────────────────────────────────────────────────────────


class TestParseReviewOutput:
    def test_plain_json(self):
        got = parse_review_output('{"decision": "allow", "risk": "low", "reason": "工作区内"}')
        assert got is not None
        assert got.allowed is True
        assert got.risk == "low"
        assert got.reason == "工作区内"

    def test_deny(self):
        got = parse_review_output('{"decision": "deny", "risk": "high", "reason": "工作区外"}')
        assert got is not None and got.allowed is False

    def test_json_inside_code_fence(self):
        """模型常把 JSON 包在 ```json 里 —— 必须能扒出来。"""
        got = parse_review_output('```json\n{"decision": "allow", "reason": "ok"}\n```')
        assert got is not None and got.allowed is True

    def test_json_with_surrounding_prose(self):
        got = parse_review_output('我的判断如下：\n{"decision": "deny", "reason": "no"}\n完毕。')
        assert got is not None and got.allowed is False

    def test_decision_case_insensitive(self):
        got = parse_review_output('{"decision": "ALLOW"}')
        assert got is not None and got.allowed is True

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "没有 JSON",
            "{不是合法 json",
            '{"risk": "low"}',                      # 缺 decision
            '{"decision": "maybe"}',                # decision 非法
            '{"decision": 123}',                    # decision 非字符串
        ],
    )
    def test_unparseable_returns_none(self, bad):
        assert parse_review_output(bad) is None

    def test_array_wrapping_single_object_is_tolerated(self):
        """模型偶尔会套一层数组。取出来是对的（宽容），不是错误。

        但**多个**对象就不会被接受 —— 那说明输出结构不符合约定，宁可判不出来（拒）。
        """
        got = parse_review_output('[{"decision": "allow"}]')
        assert got is not None and got.allowed is True
        assert parse_review_output('[{"decision": "deny"}, {"decision": "allow"}]') is None


class TestBuildReviewInput:
    def test_contains_full_context(self):
        """审查者拿不到上下文就只能瞎猜 —— 关键字段必须都在。"""
        text = build_review_input(_ctx())
        assert "write_file" in text
        assert "a.txt" in text
        assert "需要用户确认" in text
        assert "/tmp/proj" in text
        assert "帮我改一下 a.txt" in text

    def test_missing_user_intent_is_flagged(self):
        """没有授权来源时要显式提示从严判断，不能悄悄留白。"""
        text = build_review_input(ReviewContext(tool_name="bash", tool_input={}))
        assert "没有明确授权" in text


def test_policy_has_conservative_bias():
    """策略里必须有"拿不准就拒"这条偏置 —— 它是审查者的安全基础。"""
    assert "拿不准就拒" in REVIEW_POLICY
    # 也要有"授权不等于授权所有步骤"这条（防范围外推）
    assert "不等于" in REVIEW_POLICY


# ── 2. 审查者：成功路径 ──────────────────────────────────────────────


class TestReviewerSuccess:
    @pytest.mark.asyncio
    async def test_allow_passthrough(self):
        outcome = await _review(
            _FakeClient(text='{"decision": "allow", "risk": "low", "reason": "工作区内新建文件"}')
        )
        assert outcome.allowed is True
        assert outcome.reason == "工作区内新建文件"

    @pytest.mark.asyncio
    async def test_deny_passthrough(self):
        outcome = await _review(
            _FakeClient(text='{"decision": "deny", "risk": "high", "reason": "工作区外删除"}')
        )
        assert outcome.allowed is False
        assert outcome.reason == "工作区外删除"

    @pytest.mark.asyncio
    async def test_deny_without_reason_gets_fallback(self):
        """拒了但没给理由 —— 补一句，别让用户看到空白原因。"""
        outcome = await _review(_FakeClient(text='{"decision": "deny"}'))
        assert outcome.allowed is False
        assert outcome.reason  # 非空

    @pytest.mark.asyncio
    async def test_client_receives_policy_as_system_prompt(self):
        """策略要作为 system prompt 传下去（而不是塞进 user 消息）——
        这样同一 provider 上它可被前缀缓存。"""
        seen = {}

        class _C(_FakeClient):
            async def stream_chat(self, messages, system_prompt="", tools=None, system_blocks=None):
                seen["system_prompt"] = system_prompt
                seen["tools"] = tools
                yield TextChunk(text='{"decision": "allow"}')
                yield CompletionDone()

        await _review(_C())
        assert seen["system_prompt"] == REVIEW_POLICY
        # 审查者**不持有工具**：只能输出判断，不能执行操作
        assert seen["tools"] is None


# ── 3. 审查者：五路失败必须 fail-closed（最重要的一组）──────────────


class TestReviewerFailClosed:
    @pytest.mark.asyncio
    async def test_timeout(self):
        outcome = await _review(_FakeClient(text='{"decision": "allow"}', delay=0.3), timeout=0.05)
        assert outcome.allowed is False
        assert "超时" in outcome.reason

    @pytest.mark.asyncio
    async def test_exception(self):
        outcome = await _review(_FakeClient(raise_exc=RuntimeError("boom")))
        assert outcome.allowed is False
        assert "失败" in outcome.reason

    @pytest.mark.asyncio
    async def test_stream_error_event(self):
        outcome = await _review(
            _FakeClient(events=[StreamError(message="HTTP 429: rate limited")])
        )
        assert outcome.allowed is False
        assert "失败" in outcome.reason

    @pytest.mark.asyncio
    async def test_unparseable_output(self):
        outcome = await _review(_FakeClient(text="我觉得应该可以吧"))
        assert outcome.allowed is False
        assert "无法解析" in outcome.reason

    @pytest.mark.asyncio
    async def test_empty_output(self):
        outcome = await _review(_FakeClient(text=""))
        assert outcome.allowed is False

    @pytest.mark.asyncio
    async def test_missing_decision_field(self):
        """缺字段**不能**默认放行。"""
        outcome = await _review(_FakeClient(text='{"risk": "low", "reason": "看着没事"}'))
        assert outcome.allowed is False

    @pytest.mark.asyncio
    async def test_three_failure_kinds_are_distinguishable(self):
        """三种失败原因要能区分：是它判错了，还是它根本没跑起来。"""
        timeout = await _review(_FakeClient(delay=0.3, text="{}"), timeout=0.05)
        exc = await _review(_FakeClient(raise_exc=ValueError("x")))
        parse = await _review(_FakeClient(text="not json"))
        reasons = {timeout.reason, exc.reason, parse.reason}
        assert len(reasons) == 3


# ── 4. 档位与决策链 ─────────────────────────────────────────────────


def _agent(policy=None, reviewer=None, cwd=None):
    from conversation.manager import ConversationManager
    from core.agent.agent import Agent
    from core.agent.config import AgentConfig
    from core.tool.context import ExecutionContext
    from core.tool.tools import get_default_registry

    class _Cfg:
        model = "x"
        context_window = 200000

    class _Client:
        config = _Cfg()

    agent = Agent(
        registry=get_default_registry(),
        llm_client=_Client(),
        # cwd 可传：要测"工作区内写"就得让工具目标落在 cwd 里，
        # 否则权限层会直接判 deny（工作区外），根本进不到 ask 分支。
        exec_ctx=ExecutionContext(
            cwd=cwd or Path(tempfile.mkdtemp()), session_id="main"
        ),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=3),
    )
    if policy is not None:
        agent.set_unattended_policy(policy)
    if reviewer is not None:
        agent.set_approval_reviewer(reviewer)
    return agent


def test_review_is_a_valid_policy_value():
    from config.loader import _parse_host_config

    cfg = _parse_host_config({"unattended_policy": "review"})
    assert cfg.unattended_policy == "review"


def test_review_policy_is_kept_as_ask_by_check_permission():
    """`REVIEW` 档**不能在同步的权限层被代答** —— 否则审查根本轮不到。"""
    from llm.stream_events import ToolUse

    agent = _agent(policy="review")
    decision = agent._check_tool_permission(
        ToolUse(id="tu", name="write_file", input={"path": "a.txt", "content": "x"})
    )
    assert decision.effect == "ask"


def test_other_policies_still_answered_synchronously():
    """其余档位的同步代答行为**逐字不变**（零回归）。"""
    from llm.stream_events import ToolUse

    for policy, expected in (("deny_all", "deny"), ("allow_write", "allow"), ("allow_all", "allow")):
        agent = _agent(policy=policy)
        decision = agent._check_tool_permission(
            ToolUse(id="tu", name="write_file", input={"path": "a.txt", "content": "x"})
        )
        assert decision.effect == expected, policy


def test_review_falls_back_to_deny_when_called_directly():
    """`unattended_decide` 的契约是"绝不返回 ask"；`REVIEW` 传进去要落保守兜底。"""
    assert unattended_decide(UnattendedPolicy.REVIEW, "command") == "deny"
    assert unattended_decide(UnattendedPolicy.REVIEW, "write") == "deny"


def test_should_review_requires_both_reviewer_and_policy():
    assert _agent(policy="review")._should_review_approval() is False
    assert _agent(reviewer=object())._should_review_approval() is False
    assert _agent(policy="review", reviewer=object())._should_review_approval() is True
    assert _agent(policy="deny_all", reviewer=object())._should_review_approval() is False


def test_recent_user_messages_only_returns_user_role():
    """授权来源只能是**用户**说的 —— assistant 自述或工具结果不能当授权。"""
    agent = _agent(policy="review")
    conv = agent._conversation
    conv.add_user_message("第一句")
    conv.add_assistant_message("我要写文件")
    conv.add_user_message("第二句")
    assert agent._recent_user_messages() == ["第一句", "第二句"]


def test_recent_user_messages_handles_block_content():
    """content 可能是块数组（内部消息格式），不能因此丢掉授权来源。"""
    agent = _agent(policy="review")
    agent._conversation.add_user_message([{"type": "text", "text": "块里的授权"}])
    assert agent._recent_user_messages() == ["块里的授权"]


def test_recent_user_messages_respects_limit():
    agent = _agent(policy="review")
    for i in range(6):
        agent._conversation.add_user_message(f"第{i}句")
    got = agent._recent_user_messages(limit=3)
    assert got == ["第3句", "第4句", "第5句"]  # 最近 3 条，保持时间序


# ── 5. 装配 ─────────────────────────────────────────────────────────


def test_build_reviewer_uses_haiku_alias_when_present():
    from config.model import ProviderConfig
    from core.agent.bootstrap import _build_approval_reviewer
    from llm.client import LLMClient

    cfg = ProviderConfig(
        name="t",
        protocol="openai",
        model="main-model",
        api_key="sk-x",
        model_aliases={"haiku": "cheap-model"},
    )
    notices: list[str] = []
    reviewer = _build_approval_reviewer(LLMClient.create(cfg), notices)
    assert reviewer is not None
    assert reviewer._client.config.model == "cheap-model"
    assert notices == []


def test_build_reviewer_warns_when_no_alias_configured():
    """没配别名不算错（仍可用），但**必须告知成本上升**，否则钱会悄悄花掉。"""
    from config.model import ProviderConfig
    from core.agent.bootstrap import _build_approval_reviewer
    from llm.client import LLMClient

    cfg = ProviderConfig(name="t", protocol="openai", model="main-model", api_key="sk-x")
    notices: list[str] = []
    reviewer = _build_approval_reviewer(LLMClient.create(cfg), notices)
    assert reviewer is not None
    assert reviewer._client.config.model == "main-model"
    assert any("主模型" in n for n in notices)


def test_build_reviewer_returns_none_on_broken_config():
    """构造失败要返回 None（由调用方降级），不能抛出来打断启动。"""
    from core.agent.bootstrap import _build_approval_reviewer

    class _Broken:
        config = None

    assert _build_approval_reviewer(_Broken(), []) is None


# ── 6. 决策链顺序（回归锁）──────────────────────────────────────────
#
# 第一版把"审查"写在了"冒泡"**前面**，于是有冒泡通道时审查者仍被调用 ——
# 与规格"能问人时优先问人"相反。被 `.workbuddy-ai/verify_review_seam.py`
# 的用例 C 抓到。这里用单测钉住，防止再写反。


async def _drive(agent, tool_uses):
    """驱动 `_execute_tools`（async generator，必须消费完才真正执行）。"""
    async for _ev in agent._execute_tools(tool_uses):
        pass


class _StubReviewer:
    def __init__(self, allowed=True, reason="stub"):
        self._allowed = allowed
        self._reason = reason
        self.seen = []

    async def review(self, ctx):
        from core.permissions.reviewer import ReviewOutcome

        self.seen.append(ctx)
        return ReviewOutcome(allowed=self._allowed, risk="low", reason=self._reason)


class _StubUpgrader:
    serving = True

    def __init__(self):
        self.seen = []

    async def request(self, req):
        self.seen.append(req)

        class _O:
            allowed = True
            reason = ""
            choice = ""

        return _O()


def _write_tu(tmp_path):
    from llm.stream_events import ToolUse

    return ToolUse(
        id="t1",
        name="write_file",
        input={"file_path": str(tmp_path / "scratch.txt"), "content": "hello"},
    )


@pytest.mark.asyncio
async def test_upgrader_takes_precedence_over_reviewer(tmp_path):
    """**有冒泡通道时不问审查者** —— 能问人时优先问人（人的判断最准）。"""
    upgrader, reviewer = _StubUpgrader(), _StubReviewer()
    agent = _agent(policy="review", reviewer=reviewer, cwd=tmp_path)
    agent._approval_upgrader = upgrader

    await _drive(agent, [_write_tu(tmp_path)])

    assert upgrader.seen, "冒泡通道应该被用到"
    assert reviewer.seen == [], "有冒泡通道时不该再问审查者"


@pytest.mark.asyncio
async def test_reviewer_used_when_no_upgrader(tmp_path):
    """没人可问时才轮到审查者，且结论真的决定执不执行。"""
    reviewer = _StubReviewer(allowed=True, reason="低风险")
    agent = _agent(policy="review", reviewer=reviewer, cwd=tmp_path)

    await _drive(agent, [_write_tu(tmp_path)])

    assert len(reviewer.seen) == 1
    assert (tmp_path / "scratch.txt").exists(), "审查放行 → 工具应真的执行"


@pytest.mark.asyncio
async def test_reviewer_enforcement_blocks_execution(tmp_path):
    """审查拒绝 → 工具**不执行**（这是这个档位存在的意义）。"""
    reviewer = _StubReviewer(allowed=False, reason="工作区外删除，不可逆")
    agent = _agent(policy="review", reviewer=reviewer, cwd=tmp_path)

    await _drive(agent, [_write_tu(tmp_path)])

    assert not (tmp_path / "scratch.txt").exists()
    assert len(reviewer.seen) == 1


@pytest.mark.asyncio
async def test_other_policy_never_touches_reviewer(tmp_path):
    """`deny_all` 档在权限层就代答了 —— 审查者连被调用都不该被调用（零回归）。"""
    reviewer = _StubReviewer(allowed=True)
    agent = _agent(policy="deny_all", reviewer=reviewer, cwd=tmp_path)

    await _drive(agent, [_write_tu(tmp_path)])

    assert reviewer.seen == []
    assert not (tmp_path / "scratch.txt").exists()
