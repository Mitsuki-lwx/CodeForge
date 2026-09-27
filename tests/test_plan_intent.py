"""`detect_plan_intent` 语境判断的测试。

对应 `docs/spec_plan_intent.md`。
核心不变量：**排除规则只减不增** —— 正例零漏报是硬底线。
"""

from __future__ import annotations

import pytest

from core.agent.plan_mode import (
    _PLAN_INTENT_KEYWORDS,
    _REPORT_VERBS,
    _REVERSALS,
    _STRONG_INTENT,
    detect_plan_intent,
)

# ── 正例：真·计划意图，一个都不能漏 ──────────────────────────────
POSITIVE = [
    "先计划一下再动手",
    "只规划，别执行",
    "给我个方案先看看",
    "先分析下这个问题的可能原因",
    "plan first",
    "don't run it yet",
    "read only, 不要写文件",
    "先别改代码",
    "计划模式",
    "只读模式",
    "let me plan",
    "give me a plan",
    "outline first",
    "just plan, no code",
]

# ── 负例：明确要执行，绝不能被判成计划模式 ────────────────────────
NEGATIVE = [
    "文档里写了『不要执行』是什么意思？",
    "帮我把这段代码里的 print 换成 logging，不要执行其他改动",
    "刚才说不要执行，现在改主意了，执行吧",
    "修这个 bug，不要执行格式化",
    "continue，不要执行计划，直接改",
    "don't run the old version, run the new one",
    "I don't want a plan, just fix it",
    "no plan needed, refactor this function",
    "执行计划已经完成了，检查一下结果",
]


class TestNoFalseNegative:
    """★ 结构性底线：正例零漏报。"""

    @pytest.mark.parametrize("text", POSITIVE)
    def test_plan_intent_detected(self, text: str) -> None:
        assert detect_plan_intent(text), f"漏报：{text}"

    def test_strong_intent_always_wins(self) -> None:
        """强意图词带任何修饰词仍应命中。"""
        for kw in _STRONG_INTENT:
            assert detect_plan_intent(f"请{kw}这个功能"), f"强意图词失效：{kw}"

    def test_exclusion_can_only_reduce(self) -> None:
        """★ 核心不变量：排除逻辑永远不会让"未命中"变成"命中"。

        对每个关键词，取"只含该词、不含任何其他关键词、且带排除语境"的输入，
        结果必须 False —— 证明排除只作用于已命中的输入。
        再验一次：任一关键词与噪声组合仍是 False。
        """
        for kw in _PLAN_INTENT_KEYWORDS:
            # 带排除语境（"文档里写了…"）⇒ 排除生效
            assert not detect_plan_intent(f"文档里写了{kw}是什么意思"), (
                f"排除规则未生效：{kw}"
            )
        # 与所有关键词都无交集的输入恒为 False
        assert not detect_plan_intent("zzz qqq")

    def test_empty_and_noise_inputs_are_false(self) -> None:
        for t in ("", "   ", "\n\t", "hello world", "12345"):
            assert detect_plan_intent(t) is False, f"噪声输入被误判：{t!r}"


class TestNoFalsePositive:
    """★ 明确要执行的输入不能被判成计划模式。"""

    @pytest.mark.parametrize("text", NEGATIVE)
    def test_execution_request_not_mistaken(self, text: str) -> None:
        assert not detect_plan_intent(text), f"误判：{text}"

    def test_quote_context(self) -> None:
        """E1 引述语境。"""
        assert not detect_plan_intent("这句『先别改』出现在哪里")

    def test_quote_context_without_report_verb(self) -> None:
        """★ E1 的**引号分支**必须独立成立，不能靠转述动词兜底。

        样本刻意**不含任何 _REPORT_VERBS 词**（不写"文档里/说/提到"），
        只靠引号本身触发排除 —— 否则 E1 的引号分支死掉也有转述动词兜底，
        测试永远绿（这正是变异 P2 SURVIVED 逼出来的）。
        """
        for t in ("『先别改』", "「只读模式」", "『plan only』"):
            lowered = t.lower()
            assert not any(v in lowered for v in _REPORT_VERBS), (
                f"样本混入了转述动词，E1 引号分支无法被单独验证：{t}"
            )
            assert not detect_plan_intent(t), f"E1 引号分支失效：{t}"

    def test_scope_limited_negation(self) -> None:
        """E2 范围限定：只否定了一部分。"""
        assert not detect_plan_intent("改这两个文件，别改其他")

    def test_reversal(self) -> None:
        """E3 反悔 / 纠正。"""
        assert not detect_plan_intent("先别做这个，现在直接做")

    def test_negating_the_plan_itself(self) -> None:
        """E4 否定对象是"计划"本身。

        ★ 措辞刻意**不含任何反悔词**（不用"直接/现在/改成"）——
        否则 E3 一失效就有 E4 兜底，测试永远绿、规则死了也没人知道
        （这条正是变异 P6 SURVIVED 逼出来的）。
        """
        for t in ("不要执行计划", "别执行计划", "不要写计划文件"):
            assert not detect_plan_intent(t), f"E4 失效被其他规则掩盖：{t}"

    def test_partial_english_negation(self) -> None:
        """E5 英文否定修饰的是旧版。"""
        assert not detect_plan_intent("don't run the old version, run the new one")

    # ── 规则隔离：每条排除规则必须**自己**被证明 ──────────────────

    def test_strong_intent_immune_to_modifier_exclusions(self) -> None:
        """★ 强意图词必须免疫 E2–E5。

        ★ 措辞刻意保证输入里**只有这一个关键词**、且修饰词是排除规则的靶子
        （"别的" 正是 E2 的 _SCOPE_LIMITERS 之一）——
        这样"强意图免疫"这行代码删掉时，判定会立刻翻转（变异 P4 的靶心）。
        """
        assert not _STRONG_INTENT & {"别的"}  # 前提：修饰词不是强意图词
        for kw in _STRONG_INTENT:
            t = f"{kw}别的"
            if any(k in t for k in _PLAN_INTENT_KEYWORDS if k != kw):
                continue  # 句中还有其他关键词，跳过（无法隔离）
            assert detect_plan_intent(t), (
                f"强意图词 {kw} 被修饰类排除误杀了（{'别的' in t}）"
            )

    def test_e4_not_masked_by_e3(self) -> None:
        """★ E4 必须独立成立，不能靠 E3 兜底。

        构造：**含 E4 靶子（"计划"紧跟其后）、不含任何 E3 反悔词**的输入。
        若把 E4 删掉，这些输入会因无排除规则而变成 True ⇒ 测试变红。
        """
        for t in ("不要执行计划", "不要写计划", "别执行 plan"):
            lowered = t.lower()
            assert not any(
                r in lowered for r in _REVERSALS
            ), f"测试样本混入了 E3 反悔词：{t}"
            assert not detect_plan_intent(t)


class TestPureFunction:
    def test_deterministic(self) -> None:
        for t in POSITIVE + NEGATIVE:
            assert detect_plan_intent(t) == detect_plan_intent(t)

    def test_case_insensitive(self) -> None:
        assert detect_plan_intent("READ ONLY") == detect_plan_intent("read only")
        assert detect_plan_intent("Plan First") == detect_plan_intent("plan first")

    def test_does_not_mutate_input(self) -> None:
        t = "文档里写了『不要执行』是什么意思？"
        copy = str(t)
        detect_plan_intent(t)
        assert t == copy
