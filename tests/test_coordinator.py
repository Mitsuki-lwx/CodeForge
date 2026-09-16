"""Coordinator 包单元测试。

覆盖：双锁 4 种组合、env_truthy 解析、工具白名单、系统提示词。
"""

from __future__ import annotations

from core.coordinator import (
    COORDINATOR_ALLOWED_TOOLS,
    allowed_tools,
    env_truthy,
    is_enabled,
    system_prompt_suffix,
)


class _FakeFeatures:
    coordinator_mode = False


class _FakeCfg:
    def __init__(self, features) -> None:
        self.features = features


def test_is_enabled_requires_both_locks(monkeypatch):
    cfg_on = _FakeCfg(_FakeFeatures())
    cfg_on.features.coordinator_mode = True
    cfg_off = _FakeCfg(_FakeFeatures())

    monkeypatch.delenv("CODEFORGE_COORDINATOR_MODE", raising=False)
    # 00: 关+无env → False
    assert is_enabled(cfg_off) is False
    # 10: 开+无env → False
    assert is_enabled(cfg_on) is False

    monkeypatch.setenv("CODEFORGE_COORDINATOR_MODE", "1")
    # 01: 关+env → False
    assert is_enabled(cfg_off) is False
    # 11: 开+env → True
    assert is_enabled(cfg_on) is True


def test_is_enabled_no_cfg_uses_env_only_flag():
    # 无 cfg 时默认关（缺能力开关）
    import os

    os.environ["CODEFORGE_COORDINATOR_MODE"] = "1"
    try:
        assert is_enabled(cfg=None) is False
    finally:
        del os.environ["CODEFORGE_COORDINATOR_MODE"]


def test_env_truthy():
    assert env_truthy("1") is True
    assert env_truthy("true") is True
    assert env_truthy("YES") is True
    assert env_truthy("0") is False
    assert env_truthy("") is False
    assert env_truthy("no") is False


def test_allowed_tools_has_bash_not_write_file():
    tools = allowed_tools()
    assert "bash" in tools
    assert "write_file" not in tools
    assert "edit_file" not in tools
    assert "read_file" in tools
    assert "Agent" in tools


def test_coordinator_allowed_tools_constant():
    assert COORDINATOR_ALLOWED_TOOLS == allowed_tools()


def test_system_prompt_mentions_waiting_discipline():
    suffix = system_prompt_suffix()
    assert "Coordinator" in suffix
    assert "派遣" in suffix or "停手" in suffix
