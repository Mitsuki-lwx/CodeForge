"""T2 输出规约单测。"""

from __future__ import annotations

from benchmark.evaluators.conformance import evaluate_conformance, _is_control


def test_empty_output_is_0():
    assert evaluate_conformance({}, "")["value"] == 0.0
    assert evaluate_conformance({}, "   \n  ")["value"] == 0.0


def test_control_chars_are_0():
    assert evaluate_conformance({}, "hello\x1b[31mred\x1b[0m")["value"] == 0.0
    assert evaluate_conformance({}, "a\x00b")["value"] == 0.0


def test_ansi_escape_only():
    assert _is_control("\x1b[31m") is True
    assert _is_control("normal text") is False


def test_plain_single_block_is_1():
    assert evaluate_conformance({}, "修复了 calc.py 的 add()，pytest 全通过。")["value"] == 1.0
    assert evaluate_conformance({}, "55")["value"] == 1.0


def test_markdown_fence_stripped():
    out = "```\n3\n```"
    assert evaluate_conformance({}, out)["value"] == 1.0


def test_long_rambling_is_not_pipeable():
    rambling = "\n".join(f"line {i} of unrelated discussion text repeated several times" for i in range(80))
    assert evaluate_conformance({}, rambling)["value"] == 0.0


def test_negation_hit_is_0():
    item = {"negations": ["我不确定", "其实没跑过"]}
    assert evaluate_conformance(item, "这个我不确定，可能对")["value"] == 0.0


def test_negation_miss_keeps_1():
    item = {"negations": ["我不确定"]}
    assert evaluate_conformance(item, "pytest 全通过。")["value"] == 1.0
