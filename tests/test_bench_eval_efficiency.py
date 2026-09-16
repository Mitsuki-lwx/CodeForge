"""T4 过程效率单测。"""

from __future__ import annotations

from benchmark.evaluators.efficiency import EFFICIENCY_GATE, evaluate_efficiency


def _ok_metrics():
    return {
        "tool_calls": 5,
        "elapsed_s": 3.0,
        "usage": {"output_tokens": 200},
    }


def test_within_limits_is_1():
    r = evaluate_efficiency({}, _ok_metrics())
    assert r["value"] == 1.0
    assert r["over"] is False


def test_over_tool_calls_deducts():
    m = _ok_metrics()
    m["tool_calls"] = 60  # > 40 default
    r = evaluate_efficiency({}, m)
    assert r["over"] is True
    assert r["value"] < 1.0
    assert "tool_calls" in r["comment"]


def test_over_elapsed_deducts():
    m = _ok_metrics()
    m["elapsed_s"] = 500  # > 120 default
    r = evaluate_efficiency({}, m)
    assert r["over"] is True
    assert r["value"] < 1.0


def test_over_tokens_deducts():
    m = _ok_metrics()
    m["usage"] = {"output_tokens": 9000}  # > 4000 default
    r = evaluate_efficiency({}, m)
    assert r["over"] is True
    assert r["value"] < 1.0


def test_custom_limits_apply():
    r = evaluate_efficiency({"limits": {"max_tool_calls": 3}}, _ok_metrics())
    assert r["over"] is True  # tool_calls=5 超自定义 3


def test_missing_data_returns_none():
    r = evaluate_efficiency({}, {})  # 可观测性关闭，全 null
    assert r["value"] is None
    assert r["over"] is False


def test_gate_constant_is_09():
    assert EFFICIENCY_GATE == 0.9


def test_raw_exposed_in_comment():
    r = evaluate_efficiency({}, _ok_metrics())
    assert "tool_calls=5" in r["comment"]
    assert "elapsed_s=3" in r["comment"]
