"""权限安全命令判定:is_safe_command 的链式/管道/命令替换拦截。

对齐参考项目 mewcode permissions 的"安全前缀不可掩盖危险"原则:
  - `cmd && rm -rf /`、`cmd ; ...`、`$(...)`、反引号 → 拒绝
  - 纯安全命令 / 安全管道(echo | grep)→ 放行
"""

from __future__ import annotations

from core.permissions.rules import is_safe_command


class TestSafeCommandChainGuard:
    def test_plain_safe_allowed(self):
        assert is_safe_command("ls") is True
        assert is_safe_command("ls -la") is True
        assert is_safe_command("git status") is True

    def test_safe_pipe_allowed(self):
        # 安全管道逐段校验通过 → 放行(不误伤 echo | grep)
        assert is_safe_command("echo hi | grep hi") is True

    def test_and_chain_masked_danger(self):
        # 前缀安全但 && 链藏着危险 → 拒绝
        assert is_safe_command("echo hi && rm -rf /") is False

    def test_semicolon_chain_masked_danger(self):
        assert is_safe_command("echo hi ; rm -rf /") is False

    def test_command_substitution_rejected(self):
        assert is_safe_command("echo $(ls)") is False

    def test_shell_script_obvious_danger(self):
        assert is_safe_command("rm -rf /") is False
        assert is_safe_command("rm -rf /*") is False

    def test_empty_and_none(self):
        assert is_safe_command("") is False
        assert is_safe_command("   ") is False
