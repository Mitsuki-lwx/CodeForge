"""评测器单元测试（无网络）。"""

from pathlib import Path

from benchmark.evaluators import score_item


def test_contains_hit():
    item = {"name": "x", "metric": "contains", "expected_output": "55"}
    assert score_item(item, "答案是 55 才对")["value"] == 1.0


def test_contains_miss():
    item = {"name": "x", "metric": "contains", "expected_output": "999"}
    assert score_item(item, "答案是 55")["value"] == 0.0


def test_exact_normalized():
    item = {"name": "x", "metric": "exact", "expected_output": "Hello  World"}
    assert score_item(item, "  hello world  ")["value"] == 1.0


def test_regex_hit():
    item = {"name": "x", "metric": "regex", "regex": r"(商.*余数|余数.*商)"}
    assert score_item(item, "整除返回商，取余返回余数")["value"] == 1.0


def test_regex_miss():
    item = {"name": "x", "metric": "regex", "regex": r"(商.*余数)"}
    assert score_item(item, "只有余数没有商这个说法")["value"] == 0.0


def test_pytest_pass(tmp_path: Path):
    (tmp_path / "calc.py").write_text(
        "def add(a,b): return a+b\n", encoding="utf-8"
    )
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n"
        "def test_add(): assert add(1,2)==3\n",
        encoding="utf-8",
    )
    item = {"name": "x", "metric": "pytest_pass"}
    assert score_item(item, "done", cwd=tmp_path)["value"] == 1.0


def test_pytest_fail(tmp_path: Path):
    (tmp_path / "test_fail.py").write_text(
        "def test_bad(): assert False\n", encoding="utf-8"
    )
    item = {"name": "x", "metric": "pytest_pass"}
    assert score_item(item, "done", cwd=tmp_path)["value"] == 0.0


def test_pytest_no_cwd_returns_zero():
    item = {"name": "x", "metric": "pytest_pass"}
    assert score_item(item, "done", cwd=None)["value"] == 0.0


def test_unknown_metric():
    item = {"name": "x", "metric": "nope"}
    assert score_item(item, "x", cwd=None)["value"] == 0.0
