"""Feature flag + 工具过滤单元测试。

设计说明：参考 `mewcode.agents.tool_filter.build_teammate_tools`——团队协作工具的可见性
由 spawn 路径的 `build_teammate_tools` 重建专用 registry 实现（见 core/team/tools/），
基础过滤层（core/tool/filter.py）保持原样，不剥离既有后台任务工具（N6）。
"""

from __future__ import annotations

from core.team.feature import fork_teammate_enabled
from core.tool.filter import FilterParams, apply_agent_tool_filter


class _FakeFeatures:
    fork_teammate = False


class _FakeCfg:
    def __init__(self, features) -> None:
        self.features = features


def test_fork_teammate_enabled_true():
    f = _FakeFeatures()
    f.fork_teammate = True
    assert fork_teammate_enabled(_FakeCfg(f)) is True


def test_fork_teammate_enabled_false():
    f = _FakeFeatures()
    f.fork_teammate = False
    assert fork_teammate_enabled(_FakeCfg(f)) is False


def test_fork_teammate_enabled_missing_field():
    assert fork_teammate_enabled(_FakeCfg(None)) is False


_FAKE_TOOLS = [
    "read_file",
    "write_file",
    "glob",
    "TaskList",
    "SendMessage",
    "bash",
]


def test_base_filter_keeps_existing_tools():
    """基础过滤层不改动既有工具（TaskList/SendMessage 保留，N6 回归保护）。"""
    got = apply_agent_tool_filter(FilterParams(all=list(_FAKE_TOOLS)))
    assert "TaskList" in got
    assert "SendMessage" in got
    assert "read_file" in got
