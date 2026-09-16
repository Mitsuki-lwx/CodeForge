"""权限系统的分类归一化与模式决策测试。

重点覆盖 `normalize_tool_category`：`file` 是读写工具共用的 category，
必须靠 `is_read_only` 区分。此前写文件被兜底成 `command`，导致
`acceptEdits` 模式对写文件失效（矩阵是 write→allow，实际走 command→ask）。
"""

from __future__ import annotations

import pytest

from core.agent.agent import normalize_tool_category
from core.permissions.checker import PermissionChecker
from core.permissions.modes import PermissionMode
from core.tool.tools import get_default_registry

# ── 分类归一化 ─────────────────────────────────────────────────────


def test_file_category_splits_by_read_only():
    """`file` 必须按 is_read_only 分流：只读→read，可写→write。"""
    assert normalize_tool_category("file", True) == "read"
    assert normalize_tool_category("file", False) == "write"


def test_read_file_maps_to_read():
    """read_file（category=file, 只读）归为 read。"""
    t = get_default_registry().get("read_file")
    assert t.category() == "file"
    assert normalize_tool_category(t.category(), t.is_read_only()) == "read"


@pytest.mark.parametrize("name", ["write_file", "edit_file"])
def test_write_tools_map_to_write(name):
    """写类工具（category=file, 非只读）归为 write，而不是 command。

    这是 D1 的核心断言：修好前它们会落到 command 兜底分支。
    """
    t = get_default_registry().get(name)
    assert t.category() == "file"
    assert t.is_read_only() is False
    assert normalize_tool_category(t.category(), t.is_read_only()) == "write"


def test_bash_maps_to_command():
    """bash 仍是 command，不受本次修复影响。"""
    t = get_default_registry().get("bash")
    assert normalize_tool_category(t.category(), t.is_read_only()) == "command"


@pytest.mark.parametrize("name", ["glob", "grep"])
def test_search_tools_map_to_read(name):
    """检索类工具归为 read。"""
    t = get_default_registry().get(name)
    assert normalize_tool_category(t.category(), t.is_read_only()) == "read"


def test_plan_tool_maps_to_write():
    """ExitPlanMode 的 category 是 plan，映射为 write（既有行为，未改动）。"""
    t = get_default_registry().get("ExitPlanMode")
    assert normalize_tool_category(t.category(), t.is_read_only()) == "write"


def test_unknown_category_falls_back_to_read_or_command():
    """未识别的 category 退回兜底：只读→read，否则→command。"""
    assert normalize_tool_category("mcp", True) == "read"
    assert normalize_tool_category("mcp", False) == "command"
    assert normalize_tool_category("task", True) == "read"
    assert normalize_tool_category("task", False) == "command"


def test_default_registry_mapping_is_stable():
    """锁定内置工具的分类映射，防止再次漂移。"""
    reg = get_default_registry()
    expected = {
        "read_file": "read",
        "write_file": "write",
        "edit_file": "write",
        "bash": "command",
        "glob": "read",
        "grep": "read",
        "ExitPlanMode": "write",
    }
    actual = {
        name: normalize_tool_category(
            reg.get(name).category(), reg.get(name).is_read_only()
        )
        for name in expected
    }
    assert actual == expected


# ── 模式决策（D2：acceptEdits 必须与 default 可区分）────────────────


def _decide(mode: PermissionMode, tool_name: str, category: str, args: dict) -> str:
    checker = PermissionChecker(mode=mode)
    return checker.check(tool_name, False, category, args).effect


def test_accept_edits_allows_write_but_default_asks():
    """acceptEdits 对写文件放行，default 询问——两者必须可区分。

    修 D1 之前二者对 write_file 都返回 ask，acceptEdits 形同虚设。
    """
    args = {"file_path": "notes.txt", "content": "hi"}
    assert _decide(PermissionMode.ACCEPT_EDITS, "write_file", "write", args) == "allow"
    assert _decide(PermissionMode.DEFAULT, "write_file", "write", args) == "ask"


def test_accept_edits_still_asks_for_non_safe_command():
    """acceptEdits 只接受编辑，不放行非安全命令。"""
    args = {"command": "python build.py"}
    assert _decide(PermissionMode.ACCEPT_EDITS, "bash", "command", args) == "ask"


def test_dangerous_command_denied_in_every_mode():
    """危险命令黑名单在模式兜底之前生效，任何模式都不能绕过。"""
    args = {"command": "rm -rf /tmp/x"}
    for mode in PermissionMode:
        assert _decide(mode, "bash", "command", args) == "deny", mode


def test_safe_read_only_command_allowed_in_every_mode():
    """安全只读命令在 Layer 1 就放行，与模式无关。"""
    args = {"command": "git status"}
    for mode in PermissionMode:
        assert _decide(mode, "bash", "command", args) == "allow", mode


def test_bypass_allows_non_safe_command():
    """bypassPermissions 放行非安全命令（但危险命令仍被 Layer 1b 拦住，见上）。"""
    args = {"command": "python build.py"}
    assert _decide(PermissionMode.BYPASS, "bash", "command", args) == "allow"
