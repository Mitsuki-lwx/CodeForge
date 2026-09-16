"""共享匹配器单元测试：exact / not / regex / glob × 边界条件。"""

from __future__ import annotations

import pytest

from core.matcher import compile_matcher


@pytest.mark.parametrize(
    "op,value,subject,expected",
    [
        ("exact", "foo", "foo", True),
        ("exact", "foo", "bar", False),
        ("exact", "Bash", "bash", False),  # 大小写敏感
        ("not", "rm", "BashTool", True),
        ("not", "rm -rf", "rm -rf /etc", False),  # 包含则反向为 False
        ("not", "RM", "rm -rf", False),  # 大小写不敏感
        ("regex", "rm", "rm -rf /", True),
        ("regex", "^rm$", "rm -rf", False),  # 部分匹配，非整串
        ("glob", "*.py", "a.py", True),
        ("glob", "*.py", "a.txt", False),
    ],
)
def test_compile_matcher(op, value, subject, expected):
    assert compile_matcher(op, value).match(subject) is expected


def test_exact_negation_wraps_inner():
    # not 是"不包含"子串反向（ContainsMatcher 内层）
    assert compile_matcher("not", "git status").match("npm install")
    assert not compile_matcher("not", "git status").match("git status -s")


def test_regex_empty_always_true():
    assert compile_matcher("regex", "").match("anything")


def test_not_empty_always_true():
    assert compile_matcher("not", "").match("anything")


def test_invalid_regex_raises_value_error():
    with pytest.raises(ValueError):
        compile_matcher("regex", "~[invalid")


def test_unknown_op_raises_value_error():
    with pytest.raises(ValueError):
        compile_matcher("bogus", "x")


def test_matchers_str_roundtrip():
    assert str(compile_matcher("exact", "foo")) == "=foo"
    assert str(compile_matcher("regex", "x")) == "~x"
    assert str(compile_matcher("not", "x")) == "!x"
    assert str(compile_matcher("glob", "*.py")) == "*.py"
