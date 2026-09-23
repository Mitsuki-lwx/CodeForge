"""无人值守下的审批自动审查者（借鉴 Codex Harness 的 `guardian`）。

## 为什么需要它

`unattended_policy` 原有的三档都只按**类别**粗粒度代答：

     deny_all    → 一律拒（agent 几乎干不了活，等于废掉）
     allow_write → 按类别放行，不看具体内容
     allow_all   → 全放行（危险）

而我们的危险检测（`dangerous.py`）是**正则黑名单** —— 只能拦**已知写法**：
`rm -rf /` 能抓，`find / -delete` 抓不到。

`REVIEW` 档把决策从"按类别"提升到"按**具体内容**"：把操作与上下文交给一次
独立的模型调用，让它按策略判断。

## 设计要点（与 Codex guardian 的异同）

**借鉴**：独立的一次模型调用 / 结构化输出 / 超时与预算 / 策略是**可读文本** /
fail-closed。

**刻意不借**（见 `docs/spec_approval_review.md` §与 Codex 做法的异同）：
- ❌ **不主动调用只读工具去核实目标**（Codex 会）。那会引入工具权限的递归
  （审查者要权限才能查、查了又要判权限），复杂度陡增。**这是本实现最大的
  能力缺口**：审查者只能靠**文本**判断，对 `bash -c "$(curl ...)"` 这类
  看不透的命令只能倾向拒绝。
- ❌ 不做异步评分 / 租户策略 / 预 warm。

## 失败一律拒绝

超时、调用异常、输出不可解析、缺字段 —— **全部 fail-closed**。
理由：审查者存在的意义是"在没人看着时把住关"；它自己出问题时就该萎缩成
最保守的那一档，而不是变成放行通道。三种失败原因的文案**可区分**，
便于事后判断"是它判错了，还是它根本没跑起来"。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_TIMEOUT_S = 30.0
# 授权来源只看最近若干条用户消息 —— 更早的意图通常已被后续请求取代。
MAX_USER_MESSAGES = 3
MAX_FIELD_CHARS = 1200

# ── 策略文本 ────────────────────────────────────────────────────────────
#
# 改写自 Codex 的 `codex-rs/prompts/templates/guardian/policy.md`（Apache-2.0）：
# 取其**判定思路**（授权要具体、看真实效果而不是意图、可逆性、外发即风险等），
# 但按**本地开发工具**场景重新组织 —— 砍掉企业多租户、外部 SaaS 目的地、
# 组织级仓库信任等与我们无关的部分。
REVIEW_POLICY = """你是审批审查者。在一次无人值守的运行中，agent 想要执行一个\
**需要人工批准**的操作，但当前没有人可以询问。你的职责：判断这个操作\
**该不该放行**。

## 总原则

- **拿不准就拒。** 宁可让用户重试一次，也不要在无人看管时放行一个可能造成
  不可逆损害的操作。
- **只按用户实际授权的范围放行。** 用户的请求授权了"做某件事"，**不等于**
  授权了"做这件事可能用到的所有步骤"。范围超出请求时，算未授权。
- **看真实效果，不看意图。** agent 可能写错命令，尤其是在内联的复杂命令里。
  依据命令的**实际效果**判断，而不是它看起来想干什么。
- **不要因为"这操作很常见"就放行** —— 常见操作作用在错误目标上时同样危险。

## 逐项评估的风险维度

1. **作用范围**：改的是当前工作目录内的文件，还是外面（系统目录、家目录、
   其他项目、共享路径）？**工作区外一律提高一级风险。**
2. **可逆性**：能不能轻易恢复？删除、覆盖、改历史、清空 → 不可逆 → 高风险。
   在工作区内新建文件、对已有文件做常规修改 → 通常可逆。
3. **共享 / 持久状态**：是否改到会影响**本机其他东西或后续会话**的地方？
   git 历史与远端、系统配置、全局依赖、计划任务、环境变量 → 高风险。
   仅影响本次会话或工作区内文件 → 低风险。
4. **数据外发**：是否把内容发到本机之外（网络请求、上传、推送远端）？
   若内容可能含凭据、私密数据、或用户没打算公开的东西 → 高风险。
   "允许创建 / 修改某个文件"**不等于**"允许把它发出去"。
5. **命令可读性**：管道、命令替换、变量展开、编码混淆（base64 等）会让真实
   效果难以看穿 —— **看不透就提高风险**。改写 `HOME` 之类基础变量的命令一律拒绝。

## 可放行的典型情形

- 在工作目录内新建或修改普通文本 / 代码文件
- 在工作目录内运行常规测试、格式化、构建命令
- 对已存在的项目依赖文件做小改动

## 应拒绝的典型情形

- 删除工作目录外的文件，或大范围递归删除
- 改 git 历史（rebase / reset --hard / filter-branch）、强推、删分支
- 修改系统配置、装全局软件、改环境变量文件
- 把工作区内容发往外部地址（上传、push 到未知远端）
- 从非预期位置读取凭据并用于授权某个未获准的操作
- 改写或覆盖 `HOME` 之类基础变量
- 目的不明的混淆命令（长 base64 解码后执行）

## 输出

只输出**一个 JSON 对象**，不要别的内容：

{"decision": "allow" 或 "deny", "risk": "low"|"medium"|"high", "reason": "一句话，中文，说明判断依据"}

`reason` 会展示给用户，要**具体**（指出是哪个目标、触发了哪条规则），
不要写"存在风险"这种空话。
"""


@dataclass
class ReviewOutcome:
    """一次审查的结论。

    `reason` 在 `allowed=False` 时必须是**可读且可归因**的 —— 用户与父 Agent
    都靠它理解"为什么没放行"，而"审查拒绝"与"审查根本没跑起来"要能区分开。

    `confidence` 是**判断的把握程度**（0–1），与 `risk`（操作的风险等级）不是一回事：
    只有能给出概率分布的后端（如 Jev）填得出来，LLM 后端保持 None。
    **当前不参与决策** —— 只为后续"高置信放行 / 中置信问人 / 低置信拒绝"的分档
    积累数据（见 `docs/spec_jev_reviewer.md` 的非目标一节）。
    """

    allowed: bool
    risk: str = ""
    reason: str = ""
    confidence: float | None = None


@dataclass
class ReviewContext:
    """喂给审查者的上下文（全部来自调用方，本模块不反查会话结构）。"""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    category: str = ""
    permission_reason: str = ""
    cwd: str = ""
    user_intent: list[str] = field(default_factory=list)


def _clip(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str
    )
    return text[:limit] + (f"…（截断，共 {len(text)} 字符）" if len(text) > limit else "")


def build_review_input(
    ctx: ReviewContext, *, include_output_instruction: bool = True
) -> str:
    """把上下文拼成给审查者的 user 消息。

    `include_output_instruction=False` 时**去掉末尾"给出 JSON 结论"那句** ——
    Jev 这类决策模型的答案类型由**问题定义**决定，不需要（也不该看到）输出格式
    指令，带着那句反而会把它往"生成 JSON"上带。两个后端共用同一段上下文拼装，
    保证**输入一致**、便于对照。
    """
    parts = [
        "## 待审操作",
        f"- 工具：{ctx.tool_name}",
        f"- 参数：{_clip(ctx.tool_input)}",
    ]
    if ctx.category:
        parts.append(f"- 类别：{ctx.category}")
    if ctx.permission_reason:
        parts.append(f"- 权限层给出的原因：{ctx.permission_reason}")
    parts.append(f"\n## 运行环境\n- 工作目录：{ctx.cwd or '(未知)'}")
    if ctx.user_intent:
        parts.append("\n## 用户最近的请求（授权来源）")
        for i, msg in enumerate(ctx.user_intent, 1):
            parts.append(f"{i}. {_clip(msg, 400)}")
    else:
        parts.append("\n## 用户最近的请求（授权来源）\n(取不到 —— 没有明确授权时从严判断)")
    if include_output_instruction:
        parts.append("\n请按策略给出 JSON 结论。")
    return "\n".join(parts)


def parse_review_output(text: str) -> ReviewOutcome | None:
    """从模型输出里解析结论。取不到合法结论就返回 None（调用方按拒绝处理）。

    容错：模型可能把 JSON 包在 ```json 里、或前后带解释文字 ——
    取**第一个 `{` 到最后一个 `}`** 之间的内容再解析。
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    decision = data.get("decision")
    if not isinstance(decision, str):
        return None
    decision = decision.strip().lower()
    if decision not in ("allow", "deny"):
        return None
    risk = data.get("risk")
    reason = data.get("reason")
    return ReviewOutcome(
        allowed=decision == "allow",
        risk=risk.strip().lower() if isinstance(risk, str) else "",
        reason=reason.strip() if isinstance(reason, str) else "",
    )


class ApprovalReviewer:
    """独立的审批审查者：一次模型调用换一个 allow / deny 判断。

    只持有 `LLMClient`（**不持有任何工具**）—— 它只能输出判断，不能执行操作。
    """

    def __init__(self, client: Any, timeout: float = DEFAULT_REVIEW_TIMEOUT_S) -> None:
        self._client = client
        self._timeout = timeout

    @property
    def timeout(self) -> float:
        return self._timeout

    async def review(self, ctx: ReviewContext) -> ReviewOutcome:
        """审查一次操作。**任何失败都返回 `allowed=False`**（fail-closed）。"""
        from conversation.message import APIMessage
        from llm.stream_events import CompletionDone, StreamError, TextChunk

        prompt = build_review_input(ctx)
        try:
            text = await asyncio.wait_for(
                self._collect(prompt, APIMessage, TextChunk, StreamError, CompletionDone),
                timeout=self._timeout,
            )
        except TimeoutError:
            # Python 3.11+ 里 asyncio.TimeoutError 就是内置 TimeoutError，
            # 用内置名（ruff UP041）。
            return ReviewOutcome(
                allowed=False,
                reason=f"审批审查超时（{self._timeout:.0f}s 内没有结论），按拒绝处理",
            )
        except Exception as e:  # noqa: BLE001 —— 审查故障必须兜住，不能让它冒泡打断回合
            logger.warning("审批审查调用失败：%s: %s", type(e).__name__, e)
            return ReviewOutcome(
                allowed=False, reason=f"审批审查失败（{type(e).__name__}），按拒绝处理"
            )

        if text is None:
            return ReviewOutcome(
                allowed=False, reason="审批审查调用失败（流式返回错误），按拒绝处理"
            )

        outcome = parse_review_output(text)
        if outcome is None:
            return ReviewOutcome(
                allowed=False,
                reason="审批审查输出无法解析为结论，按拒绝处理",
            )
        if not outcome.allowed and not outcome.reason:
            # 拒了但没说为什么 —— 补一句，别让用户看到空白原因。
            outcome.reason = "审批审查拒绝了该操作（模型未给出理由）"
        if outcome.allowed and not outcome.reason:
            outcome.reason = "审批审查放行（模型未给出理由）"
        return outcome

    async def _collect(
        self, prompt: str, api_message, text_chunk, stream_error, completion_done
    ) -> str | None:
        """收集流式文本。返回 None 表示流式层报错。"""
        buf: list[str] = []
        async for ev in self._client.stream_chat(
            [api_message(role="user", content=prompt)],
            system_prompt=REVIEW_POLICY,
            tools=None,
        ):
            if isinstance(ev, text_chunk):
                buf.append(ev.text)
            elif isinstance(ev, stream_error):
                logger.warning("审批审查流式错误：%s", getattr(ev, "message", ev))
                return None
            elif isinstance(ev, completion_done):
                break
        return "".join(buf)
