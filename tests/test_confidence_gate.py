"""审批审查的置信度闸门（`core.permissions.confidence_gate`）单元测试。

对应官方 pattern "Confidence-Gated Routing"。核心是两件事：

1. **零回归** —— 不提供置信度的后端（现有 LLM 后端）行为**逐字不变**；
2. **按风险缩放阈值** —— 高风险要更高的把握才自动放行。
"""

from __future__ import annotations

import pytest

from core.permissions.confidence_gate import (
    AUTO_APPROVE_CONFIDENCE,
    AUTO_APPROVE_CONFIDENCE_HIGH_RISK,
    gate_on_confidence,
    threshold_for,
)
from core.permissions.reviewer import ReviewOutcome

# ── 1. 阈值表 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        ("high", AUTO_APPROVE_CONFIDENCE_HIGH_RISK),
        ("HIGH", AUTO_APPROVE_CONFIDENCE_HIGH_RISK),
        ("  high  ", AUTO_APPROVE_CONFIDENCE_HIGH_RISK),
        ("low", AUTO_APPROVE_CONFIDENCE),
        ("medium", AUTO_APPROVE_CONFIDENCE),
        ("", AUTO_APPROVE_CONFIDENCE),
        ("unknown", AUTO_APPROVE_CONFIDENCE),
    ],
)
def test_threshold_scales_with_risk(risk, expected):
    assert threshold_for(risk) == expected


def test_thresholds_match_official_pattern():
    """值取自官方 pattern（低风险 0.6 地板 / 高风险 0.85）。

    刻意钉住：改这两个数就要改这条测试 —— 逼着改的人说明"按我们的数据校准过了"。
    """
    assert AUTO_APPROVE_CONFIDENCE == 0.6
    assert AUTO_APPROVE_CONFIDENCE_HIGH_RISK == 0.85


# ── 2. ★ 零回归：不提供置信度 → 行为不变 ─────────────────────────


def test_no_confidence_passes_through_even_on_high_risk():
    """★ 本次最重要的约束：LLM 后端 `confidence=None` → **原样放行**。

    若有人把这条改成"拒"，整个 LLM 后端会突然开始拒绝本该放行的操作 ——
    这是引入闸门时最容易踩的回归。
    """
    allow, reason = gate_on_confidence(
        ReviewOutcome(allowed=True, risk="high", confidence=None)
    )
    assert allow is True
    assert reason == ""


@pytest.mark.parametrize("risk", ["low", "medium", "high", ""])
def test_no_confidence_passes_through_for_all_risks(risk):
    allow, reason = gate_on_confidence(
        ReviewOutcome(allowed=True, risk=risk, confidence=None)
    )
    assert (allow, reason) == (True, "")


# ── 3. 拒绝方向不看置信度 ────────────────────────────────────────


@pytest.mark.parametrize("confidence", [None, 0.0, 0.5, 0.99, 1.0])
def test_deny_is_never_overturned_by_confidence(confidence):
    """拒了就是拒了 —— 置信度再高也不该把拒绝翻成放行（方向性错误）。"""
    allow, reason = gate_on_confidence(
        ReviewOutcome(allowed=False, risk="low", confidence=confidence, reason="越权")
    )
    assert allow is False
    assert reason == ""  # 不覆盖审查者自己的拒绝理由


# ── 4. 边界值 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("risk", "confidence", "expected"),
    [
        # 低风险：0.6 是**含在内的下界**
        ("low", 0.59, False),
        ("low", 0.60, True),
        ("low", 0.61, True),
        ("medium", 0.59, False),
        ("medium", 0.60, True),
        # 高风险：要 0.85
        ("high", 0.84, False),
        ("high", 0.85, True),
        ("high", 0.60, False),
        ("high", 0.0, False),
        ("high", 1.0, True),
        # risk 缺失按低档
        ("", 0.59, False),
        ("", 0.60, True),
    ],
)
def test_gate_boundaries(risk, confidence, expected):
    allow, _ = gate_on_confidence(
        ReviewOutcome(allowed=True, risk=risk, confidence=confidence)
    )
    assert allow is expected


def test_high_risk_uses_risk_field():
    """同一个 confidence 因 `risk` 不同而结论不同 —— 判定必须看 `risk`。"""
    same = 0.70
    low, _ = gate_on_confidence(
        ReviewOutcome(allowed=True, risk="low", confidence=same)
    )
    high, _ = gate_on_confidence(
        ReviewOutcome(allowed=True, risk="high", confidence=same)
    )
    assert low is True
    assert high is False


# ── 5. 可归因 ────────────────────────────────────────────────────


def test_gate_reason_is_attributable():
    """被闸门拦下时，原因要含**实际置信度**与**所用阈值** —— 否则用户不知道为什么。"""
    allow, reason = gate_on_confidence(
        ReviewOutcome(allowed=True, risk="high", confidence=0.70)
    )
    assert allow is False
    assert "0.70" in reason  # 实际置信度
    assert "0.85" in reason  # 所用阈值
    assert "high" in reason  # 风险档
    assert "把握不足" in reason  # 说明是"拿不准"


def test_gate_reason_distinguishes_uncertainty_from_danger():
    """**"拿不准"与"发现危险"是两种拒绝** —— 措辞必须能区分。

    这是 spec §3.3 的核心主张：无人值守下中档没有"问人"的去路，
    那么这一档的价值就体现在**让用户事后能分辨**是哪一种拒绝。
    """
    uncertain = gate_on_confidence(
        ReviewOutcome(allowed=True, risk="low", confidence=0.3)
    )[1]
    danger_reason = "审批审查（Jev）：拒绝；超出授权（0.06）"
    assert "把握不足" in uncertain
    assert "把握不足" not in danger_reason
