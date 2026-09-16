"""T1 语义等价执行器单测。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from benchmark.evaluators.semantic import (
    _json_deep_eq,
    evaluate_semantic,
    run_exe,
    semantic_equiv,
)


# ── semantic_equiv 归约分支 ──────────────────────────────────────────

def test_number_approx_tolerance():
    assert semantic_equiv("3.0000001", "3.0", "number≈")[0] == 1
    assert semantic_equiv("5.0", "4.99", "number≈")[0] == 0
    assert semantic_equiv("1e-9", "0", "number≈")[0] == 1  # 容差内


def test_number_unparsable_is_fail():
    assert semantic_equiv("abc", "3", "number≈")[0] == 0


def test_set_unordered_histogram():
    assert semantic_equiv("[2,1,3]", "[1,2,3]", "set")[0] == 1
    assert semantic_equiv("aab", "aba", "set")[0] == 1  # 计重复
    assert semantic_equiv("aab", "abb", "set")[0] == 0


def test_lines_ignore_blank_whitespace():
    assert semantic_equiv("b\n\nc\n", "b\nc", "lines")[0] == 1
    assert semantic_equiv(" x ", "x", "lines")[0] == 1


def test_json_deep_and_key_order():
    assert semantic_equiv("[1,2,3]", "[1,2,3]", "json")[0] == 1
    assert semantic_equiv('{"a":1,"b":2}', '{"b":2,"a":1}', "json")[0] == 1
    assert semantic_equiv('{"a":1}', '{"a":2}', "json")[0] == 0


def test_json_deep_eq_numbers_with_tolerance():
    assert _json_deep_eq({"a": 3.0000001}, {"a": 3.0}) is True
    assert _json_deep_eq([1, 2], [2, 1]) is False  # 列表需有序


def test_unknown_kind_is_fail():
    assert semantic_equiv("a", "a", "nope")[0] == 0


# ── run_exe 执行 ─────────────────────────────────────────────────────

def test_run_exe_executes_py_script(tmp_path: Path):
    (tmp_path / "prog.py").write_text("print(2 + 3)\n", encoding="utf-8")
    # 用 cmd 显式跑解释器
    rc, out, err = run_exe(str(tmp_path), cmd=[sys.executable, "prog.py"])
    assert rc == 0
    assert out.strip() == "5"


def test_run_exe_feeds_stdin(tmp_path: Path):
    # 通过 python -c 读 stdin 的程序
    src = "import sys; print(len(sys.stdin.read()))"
    rc, out, err = run_exe(str(tmp_path), cmd=[sys.executable, "-c", src], input_text="abcd")
    assert rc == 0
    assert out.strip() == "4"


def test_run_exe_nonzero_returncode(tmp_path: Path):
    rc, out, err = run_exe(str(tmp_path), cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    assert rc == 3


# ── evaluate_semantic（三种源）───────────────────────────────────────

def test_evaluate_cases_semantic(tmp_path: Path):
    # 任务：agent 写一个加法程序 mul? 直接提供 cmd 脚本
    item = {
        "name": "sum_prog",
        "cases": [
            {"input": "1 2 3", "want": "6", "kind": "number≈"},
            {"input": "10 5", "want": "15", "kind": "number≈"},
        ],
        "cmd": [sys.executable, "-c", "print(sum(map(int, __import__('sys').stdin.read().split())))"],
    }
    res = evaluate_semantic(item, "ok", str(tmp_path))
    assert res["value"] == 1.0


def test_evaluate_cases_expect_error(tmp_path: Path):
    item = {
        "name": "div_by_zero",
        "cases": [
            {"input": "5 0", "want": "", "kind": "lines", "expect_error": True},
        ],
        "cmd": [sys.executable, "-c",
                "a,b=map(int,__import__('sys').stdin.read().split()); print(a//b)"],
    }
    res = evaluate_semantic(item, "ok", str(tmp_path))
    assert res["value"] == 1.0  # 除零 → returncode!=0 → 符合期望失败


def test_evaluate_no_cwd_text_fallback():
    # cwd=None 时用文本归约兜底，不执行
    item = {"name": "q", "reference": "答案是 42", "kind": "text"}
    res = evaluate_semantic(item, "这题的答案是 42 没错", None)
    assert res["value"] is not None


def test_evaluate_no_reference_returns_none():
    res = evaluate_semantic({"name": "q"}, "whatever", None)
    assert res["value"] is None
