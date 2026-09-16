"""steps 步骤×权重评分器单测。"""

from __future__ import annotations

from pathlib import Path

from benchmark.evaluators.steps import (
    HARD_GATE_DISCOUNT,
    VALID_STEP_CHECKS,
    evaluate_steps,
)

# ── 聚合 ───────────────────────────────────────────────────────────


def test_weighted_aggregation():
    # w=1 → 1.0, w=3 → 0.0：总分 = (1×1.0 + 3×0.0)/4 = 0.25
    item = {
        "steps": [
            {"name": "a", "check": "contains", "contains": ["h"], "weight": 1},
            {"name": "b", "check": "contains", "contains": ["zz"], "weight": 3},
        ],
    }
    res = evaluate_steps(item, "has h here", {})
    assert res["value"] == 0.25


def test_missing_steps_is_none():
    res = evaluate_steps({}, "x", {})
    assert res["value"] is None
    assert res["steps"] == []


def test_zero_total_weight_is_none():
    item = {
        "steps": [{"name": "a", "check": "contains", "contains": ["x"], "weight": 0}]
    }
    res = evaluate_steps(item, "x", {})
    assert res["value"] is None


# ── 连续分 ─────────────────────────────────────────────────────────


def test_continuous_contains():
    # 命中 1/2 → 0.5
    item = {
        "steps": [
            {"name": "a", "check": "contains", "contains": ["foo", "bar"], "weight": 1}
        ]
    }
    assert evaluate_steps(item, "only foo here", {})["value"] == 0.5
    # 命中 0/2 → 0.0；2/2 → 1.0
    assert evaluate_steps(item, "none", {})["value"] == 0.0
    assert evaluate_steps(item, "foo and bar", {})["value"] == 1.0


def test_continuous_file_contains(tmp_path: Path):
    (tmp_path / "solver.py").write_text("def solve(): return 1\n", encoding="utf-8")
    item = {
        "steps": [
            {
                "name": "f",
                "check": "file_contains",
                "path": "solver.py",
                "needles": ["def solve", "return 42"],
                "weight": 1,
            },
        ]
    }
    # 命中 1/2 锚点 → 0.5（cwd 需用关键字传，第 4 参是 run_metrics）
    assert evaluate_steps(item, "", {}, cwd=str(tmp_path))["value"] == 0.5


def test_exact_normalized():
    item = {
        "steps": [{"name": "e", "check": "exact", "want": "HELLO world", "weight": 1}]
    }
    assert evaluate_steps(item, "  hello   WORLD  ", {})["value"] == 1.0


def test_negates_any_forbidden_zero():
    item = {
        "steps": [
            {
                "name": "n",
                "check": "negates",
                "forbidden": ["bomb", "boom"],
                "weight": 1,
            }
        ]
    }
    assert evaluate_steps(item, "a bomb here", {})["value"] == 0.0
    assert evaluate_steps(item, "all clear", {})["value"] == 1.0


def test_unknown_check_is_zero_not_raise():
    item = {"steps": [{"name": "u", "check": "bogus", "weight": 1}]}
    assert evaluate_steps(item, "x", {})["value"] == 0.0


# ── 硬门 ────────────────────────────────────────────────────────────


def test_hard_step_flag_caps_total():
    item = {
        "steps": [
            {
                "name": "g",
                "check": "contains",
                "contains": ["zz"],
                "hard": True,
                "weight": 1,
            },
            {"name": "c", "check": "contains", "contains": ["h"], "weight": 1},
        ],
    }
    res = evaluate_steps(item, "has h here", {})
    assert res["value"] == round(0.5 * HARD_GATE_DISCOUNT, 3)
    assert res["gates_failed"] is True


def test_step_gates_thresh():
    # 步分 0.25 < thresh 0.8 → 硬门失败
    item = {
        "steps": [
            {
                "name": "x",
                "check": "contains",
                "contains": ["a", "b", "c", "d"],
                "weight": 1,
            }
        ],
        "step_gates": [{"when": "x", "thresh": 0.8}],
    }
    res = evaluate_steps(item, "a", {})
    assert res["gates_failed"] is True
    assert res["value"] == 0.0


def test_gate_referencing_missing_step_no_raise():
    item = {
        "steps": [{"name": "x", "check": "contains", "contains": ["a"], "weight": 1}],
        "step_gates": [{"when": "does_not_exist", "thresh": 0.9}],
    }
    res = evaluate_steps(item, "a", {})
    assert res["gates_failed"] is False
    assert res["value"] == 1.0


# ── 契约 ───────────────────────────────────────────────────────────


def test_closed_verb_set():
    assert "contains" in VALID_STEP_CHECKS
    assert "file_contains" in VALID_STEP_CHECKS
    assert "pytest_pass" in VALID_STEP_CHECKS
    assert "cases_ratio" in VALID_STEP_CHECKS


# ── smoke 数据集真实声明集成 ───────────────────────────────────────


def test_smoke_declares_steps_on_fitting_items():
    from benchmark.datasets import get_dataset

    smoke = get_dataset("smoke")
    has = {i["name"] for i in smoke if i.get("steps")}
    # 天然可拆步骤的 3 条应声明 steps；纯问答的 explain 不应强行拆
    assert {"add_bugfix", "write_fib", "city_distance"} <= has
    assert "explain_quotient_remainder" not in has


def test_smoke_add_bugfix_hard_gate(tmp_path: Path):
    from benchmark.datasets import get_dataset

    item = next(i for i in get_dataset("smoke") if i["name"] == "add_bugfix")
    # 未修好（无通过测试）→ hard pytest 门把总分归零
    res = evaluate_steps(item, "我改了文件", {}, cwd=str(tmp_path))
    assert res["gates_failed"] is True
    assert res["value"] == 0.0
