"""T3 产物质量 AST 单测。"""

from __future__ import annotations

from pathlib import Path

from benchmark.evaluators.quality import evaluate_quality, quality


CLEAN = (
    "def add_and_mul(a, b):\n"
    "    return (a + b) * (a - b)\n"
    "\n"
    "def greet(name):\n"
    "    return 'hi ' + name\n"
)


def _write(tmp: Path, name: str, content: str, seed=None):
    (tmp / name).write_text(content, encoding="utf-8")
    return tmp


def test_clean_product_scores_1(tmp_path: Path):
    _write(tmp_path, "solver.py", CLEAN)
    r = evaluate_quality({}, str(tmp_path))
    assert r["value"] == 1.0
    assert r["comment"].startswith("产物整洁")


def test_huge_function_deducts(tmp_path: Path):
    long_fn = "def big(x):\n" + "".join(f"    a{i} = x + {i}\n" for i in range(60)) + "    return x\n"
    _write(tmp_path, "big.py", long_fn)
    r = quality(str(tmp_path), {})
    assert r["value"] < 1.0
    assert any("超 50 行" in i or "function" in i for i in r["issues"])


def test_high_cyclomatic_deducts(tmp_path: Path):
    src = "def branchy(x):\n"
    for i in range(12):
        src += f"    if x == {i}:\n        return {i}\n"
    src += "    return -1\n"
    _write(tmp_path, "branchy.py", src)
    r = quality(str(tmp_path), {})
    assert r["value"] < 1.0
    assert any("圈复杂度" in i for i in r["issues"])


def test_magic_number_reuse(tmp_path: Path):
    src = ("def f(x):\n"
           "    return x + 12\n"
           "def g(x):\n"
           "    return x * 12 + 12\n")
    _write(tmp_path, "magic.py", src)
    r = quality(str(tmp_path), {})
    assert any("魔法数" in i and "12" in i for i in r["issues"])


def test_non_snake_naming(tmp_path: Path):
    _write(tmp_path, "camel.py", "def maxValue():\n    return 1\n")
    r = quality(str(tmp_path), {})
    assert any("非蛇形" in i for i in r["issues"])


def test_seed_files_excluded(tmp_path: Path):
    # 脏代码作为 seed，不应被计入
    seed = {"bloat.py": "def x(q):\n" + "".join(f"    v{i}=q+{i}\n" for i in range(70))}
    _write(tmp_path, "bloat.py", seed["bloat.py"])
    # agent 真正写的干净文件
    _write(tmp_path, "solver.py", CLEAN)
    r = quality(str(tmp_path), seed)
    assert r["value"] == 1.0  # seed 被跳过，只剩干净 solver


def test_no_cwd_returns_none():
    r = evaluate_quality({}, None)
    assert r["value"] is None
