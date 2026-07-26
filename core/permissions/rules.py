"""规则引擎 + 安全命令检测 + 内容提取。

三级规则加载与匹配：
  1. 会话级临时规则（内存，不持久化）
  2. 项目级 .codeforge/settings.json
  3. 用户全局 ~/.codeforge/settings.json

规则格式：ToolName:pattern → allow|deny|ask
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

RuleAction = Literal["allow", "deny", "ask"]
RuleSource = Literal["session", "project", "user"]


@dataclass
class Rule:
    """单条权限规则。"""
    tool_pattern: str   # "BashTool" or "*"
    content_pattern: str  # "rm -rf" or "*.py"
    action: RuleAction
    source: RuleSource


# ── 安全只读命令白名单 ──────────────────────────────────────

_SAFE_COMMANDS: frozenset[str] = frozenset({
    "ls", "dir", "cat", "head", "tail", "less", "more",
    "grep", "find", "echo", "printf", "pwd", "which", "whereis", "type",
    "wc", "sort", "uniq", "cut", "tr", "tee",
    "git", "hg", "svn",       # VCS: status/log/diff 是安全的
    "date", "env", "printenv", "whoami", "hostname", "uname",
    "df", "du", "stat", "file", "readlink",
    "python --version", "node --version", "pip list", "pip show",
    "docker ps", "docker images", "docker inspect",
    "npm list", "npm view",
    "cargo --version", "rustc --version", "go version",
    "man", "info", "help",
    "tree", "lsof", "ps", "top", "htop", "uptime",
})

_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "tag", "remote",
    "config", "rev-parse", "rev-list", "ls-files", "describe",
    "stash list", "stash show",
    "blame", "shortlog", "reflog",
})

_DESTRUCTIVE_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "commit", "add", "rm", "mv", "reset", "checkout", "switch",
    "restore", "merge", "rebase", "cherry-pick", "stash", "stash pop",
    "stash apply", "stash drop", "clean", "gc", "prune", "push",
    "fetch", "pull", "clone", "init", "submodule", "worktree",
})


def is_safe_command(command: str) -> bool:
    """判断命令是否为安全只读操作。

    解析 pipes 和重定向，逐段判断每段命令是否安全。
    安全 = 所有段都是安全命令且不包含破坏性参数。
    """
    if not command or not command.strip():
        return False

    cmd_stripped = command.strip()

    # 1. 先检查整体危险模式（避免分段绕过）
    if re.search(r">\s*/dev/(sd|nvme|loop|md|dm)", cmd_stripped):
        return False
    if re.search(r"<\s*/dev/(sd|nvme|loop|md|dm)", cmd_stripped):
        return False

    # 2. 提取管道前的最后一段也检查（`echo hi | bash` 中 bash 不安全）
    segments = _split_pipes(cmd_stripped)

    for seg in segments:
        # 去掉重定向部分，只检查命令体
        cmd_body, _redirects = _split_redirects(seg.strip())
        if not cmd_body.strip():
            continue

        if not _is_safe_segment(cmd_body.strip()):
            return False

    return True


def _split_pipes(command: str) -> list[str]:
    """将管道命令拆分为独立段。"""
    # 简单按 | 拆分（不处理引号内的 |）
    segments = []
    current = []
    in_single = False
    in_double = False
    for ch in command:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == '|' and not in_single and not in_double:
            segments.append(''.join(current))
            current = []
            continue
        current.append(ch)
    if current:
        segments.append(''.join(current))
    return segments if segments else [command]


def _split_redirects(command: str) -> tuple[str, list[str]]:
    """分离命令体和重定向部分。返回 (cmd_body, [redirect_parts])。"""
    # 简单按 > / < / >> 拆分
    parts = re.split(r"(\s*(?:>>|>|<)\s*\S+)", command.strip())
    if not parts:
        return "", []
    body = parts[0].strip()
    redirects = [p.strip() for p in parts[1:] if p.strip()]
    return body, redirects


def _is_safe_segment(seg: str) -> bool:
    """判断单个命令段是否安全。"""
    parts = seg.split()
    if not parts:
        return False

    base_cmd = parts[0]

    # VCS 命令特殊处理
    if base_cmd == "git" and len(parts) >= 2:
        subcmd = ' '.join(parts[1:3]) if len(parts) >= 3 else parts[1]
        if subcmd in _SAFE_GIT_SUBCOMMANDS:
            return True
        if subcmd in _DESTRUCTIVE_GIT_SUBCOMMANDS:
            return False
        # 未知 git 子命令 → 按 git 本身判断
        return True

    # 完整命令匹配（如 "python --version"）
    full_cmd = ' '.join(parts[:2]) if len(parts) >= 2 else parts[0]
    if full_cmd in _SAFE_COMMANDS:
        return True

    # 基础命令匹配
    if base_cmd in _SAFE_COMMANDS:
        return True

    return False


# ── 内容提取 ────────────────────────────────────────────────

def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    """从工具参数中提取用于规则匹配的内容文本。

    - BashTool: 提取 command
    - ReadFile / EditFile / WriteFile / Glob / Grep: 提取 file_path 或 path 参数
    - 其他: 尝试拼接参数摘要
    """
    if not arguments:
        return ""

    # 命令类工具
    if "command" in arguments:
        content = str(arguments["command"])
        # 取第一行（shell 命令）
        return content.split("\n")[0].strip()

    # 文件类工具
    for key in ("file_path", "path", "pattern"):
        if key in arguments:
            return str(arguments[key])

    # 回退：拼接参数摘要
    parts = []
    for k, v in arguments.items():
        sv = str(v)
        if len(sv) > 80:
            sv = sv[:77] + "..."
        parts.append(f"{k}={sv}")
    return ", ".join(parts) if parts else tool_name


# ── 规则引擎 ────────────────────────────────────────────────

@dataclass
class RuleEngine:
    """三级规则引擎。

    优先级：session > project > user
    匹配逻辑：tool_pattern 匹配工具名，content_pattern 匹配内容
    * 为通配符
    """

    session_rules: list[Rule] = field(default_factory=list)
    project_rules: list[Rule] = field(default_factory=list)
    user_rules: list[Rule] = field(default_factory=list)

    def add_session_rule(self, tool_pattern: str, content_pattern: str, action: RuleAction) -> None:
        """添加会话级临时规则（HITL 选择"本会话允许"后）。"""
        self.session_rules.append(Rule(
            tool_pattern=tool_pattern,
            content_pattern=content_pattern,
            action=action,
            source="session",
        ))

    def load_project_rules(self, cwd: str | Path) -> None:
        """加载项目级 .codeforge/settings.json 的 rules。"""
        path = Path(cwd) / ".codeforge" / "settings.json"
        self.project_rules = _load_rules_from_file(path, "project")

    def load_user_rules(self) -> None:
        """加载用户全局 ~/.codeforge/settings.json 的 rules。"""
        path = Path.home() / ".codeforge" / "settings.json"
        self.user_rules = _load_rules_from_file(path, "user")

    def evaluate(self, tool_name: str, content: str) -> RuleAction | None:
        """按优先级匹配规则，返回首个命中的 action。

        Returns:
            action if matched, None if no rule matches
        """
        # 优先级：session > project > user
        for rules in (self.session_rules, self.project_rules, self.user_rules):
            for rule in rules:
                if _rule_matches(rule, tool_name, content):
                    return rule.action
        return None


def _rule_matches(rule: Rule, tool_name: str, content: str) -> bool:
    """检查单条规则是否匹配。"""
    # 工具名匹配
    if rule.tool_pattern != "*" and rule.tool_pattern != tool_name:
        return False

    # 内容模式匹配
    if rule.content_pattern == "*":
        return True

    # 简单子串匹配（大小写不敏感）
    lowered_content = content.lower()
    lowered_pattern = rule.content_pattern.lower()
    if lowered_pattern in lowered_content:
        return True

    # glob 风格匹配：*.py → 匹配以 .py 结尾的路径
    if rule.content_pattern.startswith("*."):
        ext = rule.content_pattern[1:]  # ".py"
        if lowered_content.endswith(ext):
            return True
    if rule.content_pattern.startswith("*/"):
        path_part = rule.content_pattern[2:]  # "etc/*"
        if lowered_content.startswith(path_part.rstrip("*")):
            return True

    return False


def _load_rules_from_file(path: Path, source: RuleSource) -> list[Rule]:
    """从 JSON 文件加载规则列表。格式损坏时返回空列表。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    rules = []
    permissions = data.get("permissions", {})
    for entry in permissions.get("rules", []):
        try:
            rules.append(Rule(
                tool_pattern=entry.get("tool", "*"),
                content_pattern=entry.get("pattern", "*"),
                action=entry.get("action", "ask"),
                source=source,
            ))
        except (KeyError, TypeError):
            continue
    return rules
