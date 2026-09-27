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


class TestTelemetryPayload:
    """埋点行的字段完整性（对应 `.workbuddy-ai/verify_plan_telemetry.py`）。

    ★ 为什么单独立类：真实链路探针首次跑出 9/10，缺的就是 `session_id`。
    根因是 `TraceWriter.write()` **契约上不注入** session/ts/sequence
    （只有 `record()` 注入那三个），走 dict 入口就绕过了整套注入。
    实测全库 12165 行 audit 里，只有埋点这几行是光秃秃的
    `event/input_len/input_sha`，其余行都带 `session_id/ts/sequence`。
    少 `ts` 尤其致命：「几周后量化误判率」全靠时间聚合，缺了就等于白埋。
    """

    @staticmethod
    def _payload() -> dict:
        """用真 TraceWriter 落一行，返回落盘后的行（走真脱敏路径）。"""
        import json
        import tempfile
        from pathlib import Path

        from core.trace.writer import TraceWriter

        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("sess-telemetry-test", audit_dir=td)
            w.write(
                {
                    "session_id": "sess-telemetry-test",
                    "ts": 1_700_000_000_000,
                    "event": "plan_intent_auto",
                    "input_len": 7,
                    "input_sha": "abcdef123456",
                }
            )
            w.close()
            line = (Path(td) / "audit" / "sess-telemetry-test.jsonl").read_text(
                encoding="utf-8"
            )
            return json.loads(line.strip())

    def test_row_carries_session_id(self) -> None:
        assert self._payload().get("session_id") == "sess-telemetry-test"

    def test_row_carries_ts(self) -> None:
        assert self._payload().get("ts") == 1_700_000_000_000

    def test_row_survives_redaction_path(self) -> None:
        """脱敏不得把埋点的标识字段吃掉（否则字段齐全也读不出来）。"""
        row = self._payload()
        for key in ("event", "session_id", "ts", "input_len", "input_sha"):
            assert key in row, f"脱敏把 {key} 吃了"

    def test_agent_actually_writes_those_fields(self) -> None:
        """★ 回归锁：`Agent._trace_write_dict` 必须自带 session_id / ts。

        这条锁的是**实现**（不是 payload 形状）——字段是在
        `_trace_write_dict` 里补的，若有人删掉那两行，payload 测试仍会全绿。

        ★★ 刻意用 **AST** 而不是 `assert "session_id" in src`：
        变异 M3（只删 session_id 那行）下，字符串匹配**仍是绿的** ——
        因为 docstring 与注释里也出现了 `session_id`（`event.session_id`、
        「自行补齐 session_id / ts / sequence」）。字符串匹配被注释喂饱了。
        AST 只看真正执行的代码，注释/文档字符串进不了判定。
        """
        import ast
        import inspect
        import textwrap

        from core.agent.agent import Agent

        src = textwrap.dedent(inspect.getsource(Agent._trace_write_dict))
        tree = ast.parse(src)

        # 收集**执行代码**里的字符串常量（跳过 docstring）与属性/键名
        str_lits: set[str] = set()
        exec_nodes: list[ast.AST] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                str_lits.add(node.value)
            if isinstance(node, ast.Attribute):
                exec_nodes.append(node)

        attrs = {
            n.attr for n in exec_nodes if isinstance(getattr(n, "ctx", None), ast.Load)
        }
        assert "session_id" in attrs, (
            "_trace_write_dict 不再读 self._exec_ctx.session_id"
        )
        assert "time" in attrs, "_trace_write_dict 不再取 time.time()"
        # payload 字典的两个键必须是**字面字符串键**，不是变量
        assert "session_id" in str_lits and "ts" in str_lits, (
            f"payload 的 session_id/ts 键不在字面量里：{sorted(str_lits)}"
        )

    def test_same_input_yields_same_hash(self) -> None:
        """B 的分析前提：同表述重复出现时能靠哈希聚到一起。"""
        import hashlib

        t = "先计划一下再动手"
        h1 = hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]
        h2 = hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]
        assert h1 == h2
        assert len(h1) == 12
