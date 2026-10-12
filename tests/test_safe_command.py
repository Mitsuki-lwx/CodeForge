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


class TestGitSubcommandGuard:
    """git 子命令判定 —— 修的是一个 **fail-open** 漏洞。

    `docs/spec_review_rollout_gaps.md` §3：旧实现用 `' '.join(parts[1:3])`
    取子命令，于是 `git commit -m x` 取到 `'commit -m'` —— 安全表与危险表**都不匹配**
    → 落到"未知子命令 = 安全"的分支。

    后果：`git reset --hard` / `git clean -fdx` / `git push --force` 全被当成
    **只读命令直接放行**，连 `deny_all` 档都绕得过去（审批、审查者、置信度闸门
    全被跳过，因为权限层在这里就说了 allow）。

    注意 `commit` / `reset` / `push` **本来就在危险表里** —— 表没错，**取词取错了**。
    """

    def test_known_safe_subcommands_still_allowed(self):
        """零回归：真只读的子命令照旧放行。"""
        assert is_safe_command("git status") is True
        assert is_safe_command("git log --oneline -5") is True
        assert is_safe_command("git diff HEAD") is True
        # 两词形式存在的意义：区分 `stash list`（安全）与 `stash`（危险）
        assert is_safe_command("git stash list") is True

    def test_destructive_subcommand_with_flags_not_safe(self):
        """带选项的危险子命令 —— 旧实现全被判成"安全只读"。"""
        assert is_safe_command("git commit -m x") is False
        assert is_safe_command("git reset --hard HEAD~3") is False
        assert is_safe_command("git clean -fdx") is False
        assert is_safe_command("git push --force origin main") is False
        assert is_safe_command("git checkout -- .") is False
        assert is_safe_command("git rebase -i HEAD~5") is False

    def test_bare_destructive_subcommand_not_safe(self):
        assert is_safe_command("git stash") is False
        assert is_safe_command("git add .") is False

    def test_unknown_subcommand_is_fail_closed(self):
        """未知 git 子命令 → **fail-closed**（不再"按 git 本身判断 = 安全"）。

        代价：`git grep` 这类无害子命令也会开始问人 —— 刻意的取舍，
        **不确定就问，比不确定就放行安全**。
        """
        assert is_safe_command("git grep foo") is False
        assert is_safe_command("git unknown-thing --x") is False

    def test_git_config_not_safe(self):
        """`git config` 会写用户级配置（持久化后门），不再是"安全只读"。"""
        assert is_safe_command("git config --global alias.z '!rm -rf /'") is False
        assert is_safe_command("git config --get user.name") is False


class TestSafePrefixCannotMaskDanger:
    """安全前缀 + 分隔符/写原语/越界路径 —— 逐个钉住已修的真实漏洞。

    这些用例在修复前**全部返回 True**（被判"安全只读"直接放行、连审批都不进）。
    """

    def test_newline_separator_not_safe(self):
        """换行是命令分隔符：`ls\\nrm -rf ~` 的第二行曾完全不被检查。"""
        assert is_safe_command("ls\nrm -rf ~/important") is False
        assert is_safe_command("ls\r\nrm -rf /tmp/x") is False

    def test_ampersand_separator_not_safe(self):
        """单个 `&` 也是分隔符（旧实现只拒 `&&`）。"""
        assert is_safe_command("ls & rm -rf /tmp/x") is False
        # `2>&1` 是 fd 重定向、不是分隔符 → 不误伤
        assert is_safe_command("ls 2>&1") is True
        assert is_safe_command("ls 2>&1 | grep x") is True

    def test_write_primitives_not_safe(self):
        """`tee` 曾是白名单成员 → 任意路径写入免审批。"""
        assert is_safe_command("echo pwned | tee ~/.bashrc") is False
        assert is_safe_command("tee /etc/hosts") is False

    def test_env_dump_not_safe(self):
        """`env` / `printenv` 会把全部环境变量（含 key）倒进上下文。"""
        assert is_safe_command("env") is False
        assert is_safe_command("printenv") is False
        assert is_safe_command("env rm -rf /tmp/x") is False

    def test_output_redirect_not_safe(self):
        """输出重定向是写入，旧实现把重定向段整个丢掉、只校验命令体。"""
        assert is_safe_command("echo x > ~/.bashrc") is False
        assert is_safe_command("echo x >> /tmp/out.txt") is False
        # 丢弃到 /dev/null 无害 → 仍算安全
        assert is_safe_command("ls > /dev/null") is True
        assert is_safe_command("ls 2>/dev/null") is True

    def test_escaping_paths_not_safe(self):
        """绝对路径 / `~` / `..` 参数 → 不判安全（交给权限层去问）。"""
        assert is_safe_command("cat ~/.ssh/id_rsa") is False
        assert is_safe_command("cat /etc/passwd") is False
        assert is_safe_command("find / -name id_rsa") is False
        assert is_safe_command("grep -r secret /etc") is False
        assert is_safe_command("cat ../../outside.txt") is False
        # 工作区内的相对路径照旧放行（零回归）
        assert is_safe_command("cat notes/e2e.txt") is True
        assert is_safe_command("ls -la src") is True

    def test_find_write_flags_not_safe(self):
        """`find` 的写/执行选项把它变成删除/执行原语。"""
        assert is_safe_command("find . -name '*.py' -delete") is False
        assert is_safe_command("find . -name '*.py' -exec rm {} +") is False
        assert is_safe_command("find . -name '*.py'") is True

    def test_sort_output_flag_not_safe(self):
        assert is_safe_command("sort -o out.txt in.txt") is False
        assert is_safe_command("sort in.txt") is True

    def test_multiline_command_content_is_not_truncated(self):
        """`extract_content` 不再只取第一行 —— 权限判定/黑名单看得到全量。"""
        from core.permissions.rules import extract_content

        assert extract_content("bash", {"command": "ls\nrm -rf ~"}) == "ls\nrm -rf ~"
