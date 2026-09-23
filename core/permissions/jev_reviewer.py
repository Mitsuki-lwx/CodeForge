"""用 Jev（TypeSafe System One）做审批审查的决策后端。

## 为什么它不放 `llm/adapters/`

`Adapter` 基类的契约是 **chat + SSE 流式**（`build_request(messages, ...)` +
`emit_events(lines)`）；Jev 是 **`state` + `questions` → 一次性 typed answer**，
无流式、无工具、无对话历史。**不是同一类东西**，硬塞进去会污染那个抽象。

它的正确位置是这里 —— `ApprovalReviewer` 的**同级 Provider**：两者实现同一个
`review(ctx) -> ReviewOutcome`，`agent.py` 无差别调用
（见 `docs/spec_jev_reviewer.md` §3.1 的 seam 三角色）。

## ★ 只问原子维度，结论由代码组合（实测教训）

**刻意不问一个笼统的 "verdict"（放行 / 拒绝）。**

2026-09-23 实测：第一版在三个原子维度之外，还额外问了一个"该放行还是该拒绝"。
那个复合判断在**低风险场景上摇摆** —— 同一输入两次调用给出 `allow` / `deny`
两种结论，其 `confidence` 只有 0.09 / 0.06；而三个原子维度
（风险 low / 在授权内 / 可逆）**两次完全一致**。

官方的"原子问题"原则正好点名了这个错法：

    If the question you want to ask would require extended reasoning or weighs
    multiple independent factors, decompose it. Ask each factor as a separate
    question, then combine the results with logic in your code.

我既拆了维度、又让它把维度**重新权衡一遍** —— 等于把拆维度的好处抵消掉。
现在：**只问三个单因素维度，结论由 `decide_from_dimensions` 组合**。

## reason 由代码拼

Jev **不生成文本**，没有自由文本理由 —— 我们自己用**结构化数值**拼一句可归因
的中文。这比 LLM 的文本**更可复查**：数字是答案的一部分，不是模型"讲"出来的。

## 延迟

实测 1.2–9.7s（另有一次 >90s 超时，复测属偶发）。审查**阻塞在工具执行的关键
路径**上，所以这是本后端最大的风险 —— 见 `DEFAULT_JEV_TIMEOUT_S`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from core.permissions.reviewer import (
    ReviewContext,
    ReviewOutcome,
    build_review_input,
)

logger = logging.getLogger(__name__)

DEFAULT_JEV_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"

# 实测最慢 9.7s（另有偶发 >90s）。审查在工具执行的关键路径上，卡太久等于拖慢
# agent —— 15s 覆盖实测最慢并留余量；超时一律 fail-closed。可通过配置覆盖。
DEFAULT_JEV_TIMEOUT_S = 15.0

# ── 策略：三个**单因素**问题（判定原则提炼自 `reviewer.REVIEW_POLICY`）────
#
# 不再有 "verdict" —— 见模块 docstring 的实测教训。
JEV_QUESTIONS: dict[str, dict[str, Any]] = {
    "in_scope": {
        "type": "noul",
        "instructions": (
            "这个操作确实落在用户最近请求所授权的范围内 ——"
            "而不是它可能顺带需要的额外步骤。"
            "注意：用户的请求授权了做某件事，不等于授权了做这件事可能用到的所有步骤。"
        ),
    },
    "reversible": {
        "type": "noul",
        "instructions": (
            "如果这个操作出错，后果可以轻易恢复（可逆）。"
            "删除、覆盖、改写历史、清空、推送到远端这类都算不可逆。"
        ),
    },
    "risk_level": {
        "type": "score",
        "instructions": "这个操作的风险等级有多高？依据它的**实际效果**判断，而不是它看起来想干什么。",
        "criteria": [
            "低：工作区内的常规读写、测试或构建，出错容易恢复",
            "中：涉及工作区外的路径、共享状态，或不可逆但不致命",
            "高：大范围删除、改写 git 历史或强推、修改系统配置、向外泄露数据",
        ],
    },
}

# risk_level 的 Score 有三级（0/1/2），score 是概率加权位置。
# 边界取 2/3 与 4/3（三级均匀分布下的分界）。
_RISK_LOW_MAX = 2 / 3
_RISK_MEDIUM_MAX = 4 / 3


def build_jev_request(ctx: ReviewContext, *, model: str = DEFAULT_JEV_MODEL) -> dict:
    """组装 `/v1/systemone` 的请求体。

    `state` 复用 LLM 后端同一段上下文拼装（去掉输出格式指令）—— 保证两个后端
    **输入一致**，判定结果才可对照。
    """
    return {
        "state": build_review_input(ctx, include_output_instruction=False),
        "model": model,
        "questions": JEV_QUESTIONS,
    }


def _noul(answer: Any) -> float | None:
    """取 Noul 答案的 0–1 概率；取不到返回 None（`decide` 会把 None 当不安全）。"""
    if isinstance(answer, dict):
        value = answer.get("noul")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _score(answer: Any) -> tuple[float | None, float | None]:
    """从 Score 答案里取 (分数, 置信度)。"""
    if not isinstance(answer, dict):
        return None, None
    raw_score = answer.get("score")
    raw_conf = answer.get("confidence")
    score = float(raw_score) if isinstance(raw_score, (int, float)) else None
    conf = float(raw_conf) if isinstance(raw_conf, (int, float)) else None
    return score, conf


def risk_from_score(score: float) -> str:
    """Score(0–2) → `low` / `medium` / `high`（与现有 `ReviewOutcome.risk` 一致）。"""
    if score < _RISK_LOW_MAX:
        return "low"
    if score < _RISK_MEDIUM_MAX:
        return "medium"
    return "high"


def decide_from_dimensions(
    risk_score: float | None,
    in_scope: float | None,
    reversible: float | None,
) -> bool:
    """由三个**原子维度**组合出结论。

    **fail-closed**：三项都必须明确成立才放行 —— 任一项不满足、或取不到值，都拒。

    这不是"多数表决"。无人值守场景下"有一项说不清"就等于"不该放行"：
    一个在授权内、可逆，但**风险说不清**的操作，同样不该在没人看着时跑。
    """
    if in_scope is None or in_scope < 0.5:
        return False
    if reversible is None or reversible < 0.5:
        return False
    return not (risk_score is None or risk_score >= _RISK_MEDIUM_MAX)


def _certainty(p: float) -> float:
    """Noul 概率的"明确程度"：0.5 = 完全不确定 → 0；1.0 或 0.0 = 很确定 → 1。"""
    return abs(p - 0.5) * 2


def compute_confidence(
    risk_confidence: float | None,
    in_scope: float | None,
    reversible: float | None,
) -> float | None:
    """置信度：取三个维度里**最薄弱**的那个（本模块自己的度量）。

    Jev 的 `confidence` 只出现在 Choice/Score 答案上，Noul 没有 —— 而我们只用
    Noul + Score，所以拿不到现成的"整体置信度"。官方明确说不锁定他们的定义
    （"you are never locked into our definition ... which is exactly why we give
    you the full probabilities"），于是这里取「三个维度中确定性最低者」作为
    保守代理：**一个维度拿不准，整体就不该被高看**。

    这个值目前**不参与决策**，只为后续"高置信放行 / 中置信问人 / 低置信拒绝"
    的分档积累数据（见 `docs/spec_jev_reviewer.md` 的非目标一节）。
    """
    vals: list[float] = []
    if risk_confidence is not None:
        vals.append(max(0.0, min(1.0, risk_confidence)))
    if in_scope is not None:
        vals.append(_certainty(in_scope))
    if reversible is not None:
        vals.append(_certainty(reversible))
    return min(vals) if vals else None


def compose_reason(
    allowed: bool,
    risk_score: float | None,
    in_scope: float | None,
    reversible: float | None,
) -> str:
    """用**结构化数值**拼一句可归因的中文（Jev 不生成文本）。

    会展示给用户，所以要求具体 —— 与 LLM 后端不同，这里不可能出现"存在风险"
    这类空话，因为每个词都对应一个可复查的数。
    """
    parts = [f"审批审查（Jev）：{'放行' if allowed else '拒绝'}"]
    if risk_score is not None:
        parts.append(f"风险 {risk_from_score(risk_score)}（{risk_score:.2f}/2）")
    if in_scope is not None:
        parts.append("在授权内" if in_scope >= 0.5 else f"超出授权（{in_scope:.2f}）")
    if reversible is not None:
        parts.append("可逆" if reversible >= 0.5 else f"不可逆（{reversible:.2f}）")
    return "；".join(parts)


def parse_jev_response(data: Any) -> ReviewOutcome:
    """把 typed answer 映射成结论。

    **任何不合法都抛异常**，由 `JevReviewer.review` 兜成 fail-closed ——
    审查者宁可拒绝，也不能因为"响应看不懂"而变成放行通道。
    """
    if not isinstance(data, dict):
        raise TypeError(f"响应不是对象（{type(data).__name__}）")

    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise TypeError("响应缺少 answers")

    risk_score, risk_conf = _score(answers.get("risk_level"))
    in_scope = _noul(answers.get("in_scope"))
    reversible = _noul(answers.get("reversible"))

    if risk_score is None and in_scope is None and reversible is None:
        # 三个维度全缺 = 响应结构不对（不是"模型保守"），必须区分出来。
        raise ValueError("三个维度全部缺失，无法判断")

    allowed = decide_from_dimensions(risk_score, in_scope, reversible)
    return ReviewOutcome(
        allowed=allowed,
        risk=risk_from_score(risk_score) if risk_score is not None else "",
        reason=compose_reason(allowed, risk_score, in_scope, reversible),
        confidence=compute_confidence(risk_conf, in_scope, reversible),
    )


class JevReviewer:
    """用 Jev 判断 `ask` 级操作。

    接口与 `ApprovalReviewer` **完全一致**（`review(ctx) -> ReviewOutcome`），
    所以 `agent.py` 不需要知道用的是哪一个。

    与 LLM 后端同样的边界：**不持有任何工具** —— 它只能输出判断，不能执行操作。
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_JEV_URL,
        model: str = DEFAULT_JEV_MODEL,
        timeout: float = DEFAULT_JEV_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise ValueError("JevReviewer 需要 api_key")
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._timeout = timeout

    @property
    def timeout(self) -> float:
        return self._timeout

    def _post(self, payload: dict) -> Any:
        """同步 POST（用 `asyncio.to_thread` 调用，避免阻塞事件循环）。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())

    async def review(self, ctx: ReviewContext) -> ReviewOutcome:
        """审查一次操作。**任何失败都返回 `allowed=False`**（fail-closed）。

        三类失败的原因文案**互不相同**，便于事后判断"是它判错了，还是它根本
        没跑起来"。
        """
        payload = build_jev_request(ctx, model=self._model)
        try:
            # urllib 是阻塞 IO → 丢线程池，别卡住事件循环。
            data = await asyncio.wait_for(
                asyncio.to_thread(self._post, payload),
                timeout=self._timeout,
            )
        except TimeoutError:
            return ReviewOutcome(
                allowed=False,
                reason=f"审批审查超时（Jev {self._timeout:.0f}s 内没有结论），按拒绝处理",
            )
        except urllib.error.HTTPError as e:
            logger.warning("Jev 审查 HTTP 错误：%s %s", e.code, e.reason)
            return ReviewOutcome(
                allowed=False,
                reason=f"审批审查调用失败（Jev HTTP {e.code}），按拒绝处理",
            )
        except Exception as e:  # noqa: BLE001 —— 审查故障必须兜住，不能打断回合
            logger.warning("Jev 审查调用失败：%s: %s", type(e).__name__, e)
            return ReviewOutcome(
                allowed=False,
                reason=f"审批审查调用失败（Jev {type(e).__name__}），按拒绝处理",
            )

        try:
            return parse_jev_response(data)
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Jev 响应不合法：%s", e)
            return ReviewOutcome(
                allowed=False,
                reason=f"审批审查输出不合法（Jev：{e}），按拒绝处理",
            )
