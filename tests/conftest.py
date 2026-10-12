"""测试全局隔离 —— 让套件不受**开发机状态**影响。

这里只放"必须在每个测试里都成立"的两件事，别的都写在各自的测试文件里。

## 1. 用户级 MCP 配置

`load_mcp_config()` 会读 `~/.codeforge/mcp.json`。开发机上真配了 MCP server 时
（比如刚跑过 `codeforge mcp add playwright`），**每个走 `build_session` 的测试**
都会去拉起那个子进程：慢（npx 冷启动几秒）、且结果取决于开发机装了什么。

所以默认把 `USER_CONFIG_PATH` 指到一个不存在的临时路径。
要测用户级配置的用例自己 patch（见 `tests/test_mcp_security.py`）。

## 2. 项目级信任开关

`CODEFORGE_TRUST_PROJECT_MCP` 若在开发机环境里被设成 `1`，项目级 MCP 配置
会在测试里被加载 —— 同样让结果依赖环境。默认清掉。
"""

from __future__ import annotations

import pytest

from core.mcp.config import TRUST_ENV_VAR


@pytest.fixture(autouse=True)
def _isolate_mcp_environment(tmp_path, monkeypatch):
    """每个测试都跑：MCP 配置来源与信任开关都回到"干净机器"的状态。"""
    monkeypatch.setattr(
        "core.mcp.config.USER_CONFIG_PATH",
        tmp_path / "isolated-user-mcp.json",  # 不存在 → 不会加载任何 server
    )
    monkeypatch.delenv(TRUST_ENV_VAR, raising=False)
