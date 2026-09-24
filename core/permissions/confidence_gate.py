"""审批审查的置信度闸门（官方 Confidence-Gated Routing）。

## 它解决什么

审查者给出的是「(放行 / 拒绝) + 置信度」。**只看 `allowed` 等于把概率塌缩成布尔**
—— Jev 文档点名的反模式：

    Teams that collapse the probability to a boolean at the first opportunity
    throw away the feature they paid for.

实测证据（2026-09-23 落地 Jev 后端时）：第一版设计**摇摆** —— 同一输入两次调用
给出 allow / deny 两种结论，其 `confidence` 只有 **0.09 / 0.06**，属于"模型真的
不确定"；但当时的实现只会"拒"，即 **"不确定"被当成了"确定拒绝"**。

官方 pattern（`docs.typesafe.ai/patterns/confidence-routing`）：

    confidence < 0.6   → 转人工（"catches anything the model is genuinely uncertain about"）
    低风险 + >=0.6     → 直接执行（最坏后果不过是重听一遍）
    高风险 + >0.85     → 才自动执行

## ★ 为什么"中档"是**拒绝**而不是「转人工」

官方三档是「执行 / 问人 / 转人工」。我们这里有个**硬约束**：

**能走到审查这一步，就说明"没有人可问"了。**

分支顺序是「冒泡 → 审查 → 人工 HITL」：

- 有冒泡通道 → 在上一分支就被拦下问人了，**根本走不到审查**
- 走到审查 = 没有冒泡通道
- 而 `REVIEW` 档只在**无人值守**（host 模式）下生效 → 人工 HITL 也没人应答

所以「问人」「转人工」**都没有去路**，中档只能拒绝。

**这不是妥协，而是把它做实**：这一档的价值体现为**区分拒绝的理由** ——
"拿不准（0.42）且无人可问" 与 "明确危险（超出授权 0.06）" 是**两种不同的拒绝**，
用户事前定的策略、事后看的日志都据此不同。

将来若有了可用的升级通道（比如 host 接一个异步人工队列），这里的「拒」可以直接
改成「升级」—— 闸门是个独立纯函数，改一处即可。

## 阈值的刻度问题（必须知道）

0.6 / 0.85 取自官方。但官方的 `confidence` 是在**他们自己的 Choice/Score 分布**上
校准的，而我们的 `confidence` 是**自己定义的度量**
（`jev_reviewer.compute_confidence`：三个维度里最薄弱者）。

**两者不是同一个刻度。** 这里借的是**思路与量级**，不是"官方数字对我们一定正确"。
阈值集中在下面两个常量里 —— 等真实运行积累出分布后，**按我们自己的数据校准**。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只用于类型标注，避免运行时 import 执行层
    from core.permissions.reviewer import ReviewOutcome

# 自动放行所需的置信度（官方 pattern 给的值，**待按我们的数据校准**）。
# 低/中风险：官方说"0.6 就够 —— 最坏后果不过是重听一遍"。
AUTO_APPROVE_CONFIDENCE = 0.6
# 高风险：官方要求"unless very high confidence, otherwise ask the user to confirm"。
AUTO_APPROVE_CONFIDENCE_HIGH_RISK = 0.85


def threshold_for(risk: str) -> float:
    """按操作风险给出自动放行所需的置信度阈值。

    `risk` 取值见 `ReviewOutcome.risk`（`low` / `medium` / `high`）。
    空值或未知值按低档处理 —— 保守性已经由 `allowed` 本身承担，
    这里不再因"缺字段"额外收紧（那会让两个后端的行为无谓地分叉）。
    """
    return (
        AUTO_APPROVE_CONFIDENCE_HIGH_RISK
        if (risk or "").strip().lower() == "high"
        else AUTO_APPROVE_CONFIDENCE
    )


def gate_on_confidence(outcome: ReviewOutcome) -> tuple[bool, str]:
    """在**允许方向**上按置信度把关。返回 `(是否放行, 补充原因)`。

    | 输入 | 输出 | 理由 |
    |---|---|---|
    | `allowed=False` | `(False, "")` | 保守方向不需要置信度兜底 |
    | `confidence is None` | `(True, "")` | **后端不提供置信度 = 行为不变**（零回归） |
    | `confidence >= threshold_for(risk)` | `(True, "")` | 够有把握 |
    | 否则 | `(False, 原因)` | 拿不准就别放行 |

    补充原因为空串时，调用方应回落到 `outcome.reason`（不丢审查者的说明）。
    """
    if not outcome.allowed:
        return False, ""

    confidence = outcome.confidence
    if confidence is None:
        # ★ 不提供置信度的后端（现有 LLM 后端）行为**逐字不变** ——
        #   刻意不给它硬造一个数字：那只会是"又一个生成的数字"，
        #   不是校准过的概率（官方也明说 confidence 要有校准意义才有用）。
        return True, ""

    threshold = threshold_for(outcome.risk)
    if confidence >= threshold:
        return True, ""

    return False, (
        f"审批审查倾向于放行，但把握不足（置信度 {confidence:.2f} < "
        f"风险 {outcome.risk or 'low'} 所需的 {threshold:.2f}）；"
        f"当前无人可问，保守起见按拒绝处理"
    )
