"""Worktree — 目录名校验 单元测试。"""

from __future__ import annotations

import re

from core.worktree.safe_name import (
    NAME_MAX_LEN,
    generate_agent_name,
    generate_wf_name,
    is_generated_name,
    is_safe_name,
)


def test_safe_valid_names():
    assert is_safe_name("my-feature")
    assert is_safe_name("agent-abc1234")
    assert is_safe_name("wf_0a1b2c3d")
    assert is_safe_name("dir/sub")  # 允许 / 嵌套
    assert is_safe_name("a_b-c")


def test_safe_rejects_empty_and_long():
    assert not is_safe_name("")
    assert not is_safe_name("a" * (NAME_MAX_LEN + 1))
    assert is_safe_name("a" * NAME_MAX_LEN)  # 边界


def test_safe_rejects_invalid_chars():
    assert not is_safe_name("bad name!")
    assert not is_safe_name("a b")
    assert not is_safe_name("a.b")


def test_safe_rejects_dot_segments():
    assert not is_safe_name(".")
    assert not is_safe_name("..")
    assert not is_safe_name("a/../b")


def test_safe_rejects_absolute():
    assert not is_safe_name("/etc/passwd")
    assert not is_safe_name("a//b")  # 空段


def test_generate_agent_name_format():
    n = generate_agent_name()
    assert re.match(r"^agent-[0-9a-f]{7}$", n), n
    assert len(n) == 13  # "agent-" = 6 + 7 hex


def test_generate_agent_name_random():
    assert generate_agent_name() != generate_agent_name()


def test_generate_wf_name_format():
    n = generate_wf_name()
    assert n.startswith("wf_")
    assert len(n) == 3 + 8  # "wf_" + 8 hex


def test_is_generated_name():
    assert is_generated_name("agent-a3f2b1c")
    assert is_generated_name("wf_0a1b2c3d")
    assert not is_generated_name("my-feature")
    assert not is_generated_name("manual-agent-x")
