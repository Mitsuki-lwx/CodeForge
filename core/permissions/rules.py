"""规则引擎 + 安全命令检测 + 内容提取。

三级规则加载与匹配：
  1. 会话级临时规则（内存，不持久化）
  2. 项目级 .codeforge/settings.json
  3. 用户全局 ~/.codeforge/settings.json

规则格式：ToolName:pattern → allow|deny|ask
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

RuleAction = Literal["allow", "deny", "ask"]
RuleSource = Literal["session", "project", "user"]


@dataclass
class Rule:
    """单条权限规则。"""

    tool_pattern: str  # "BashTool" or "*"
    content_pattern: str  # "rm -rf" or "*.py"
    action: RuleAction
    source: RuleSource


# ── 安全只读命令白名单 ──────────────────────────────────────

_SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "ls",
        "dir",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "find",
        "echo",
        "printf",
        "pwd",
        "which",
        "whereis",
        "type",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "git",
        "hg",
        "svn",  # VCS: status/log/diff 是安全的
        "date",
        "whoami",
        "hostname",
        "uname",
        "df",
        "du",
        "stat",
        "file",
        "readlink",
        "python --version",
        "node --version",
        "pip list",
        "pip show",
        "docker ps",
        "docker images",
        "docker inspect",
        "npm list",
        "npm view",
        "cargo --version",
        "rustc --version",
        "go version",
        "man",
        "info",
        "help",
        "tree",
        "lsof",
        "ps",
        "top",
        "htop",
        "uptime",
    }
)
# ⚠️ 刻意**不在**上表里的三个命令（曾经在，属 fail-open）：
#   * `tee`      —— 任意路径写原语：`echo x | tee ~/.bashrc` 曾被判"安全只读"。
#   * `env`      —— 命令执行原语：`env rm -rf x` 合法且首 token 是 env。
#   * `printenv` —— 把全部环境变量（含 API key）倒进 LLM 上下文。
# 移除后它们落到权限矩阵的 `ask`，不再免审批执行。

_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "tag",
        "remote",
        "rev-parse",
        "rev-list",
        "ls-files",
        "describe",
        "stash list",
        "stash show",
        "blame",
        "shortlog",
        "reflog",
    }
)
# 注：`config` 已移出安全表 —— `git config --global ...` 会写用户级配置（可做持久化后门），
# 不是只读操作；移除后 `git config` 走"未知子命令 → fail-closed"分支，落到 `ask`。

_DESTRUCTIVE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "commit",
        "add",
        "rm",
        "mv",
        "reset",
        "checkout",
        "switch",
        "restore",
        "merge",
        "rebase",
        "cherry-pick",
        "stash",
        "stash pop",
        "stash apply",
        "stash drop",
        "clean",
        "gc",
        "prune",
        "push",
        "fetch",
        "pull",
        "clone",
        "init",
        "submodule",
        "worktree",
    }
)


# ── 安全判定的三条硬规则（fail-closed）────────────────────────────────
#
# 这三条修的都是"安全前缀掩盖危险"的同类漏洞，判据统一为：**拿不准就不判安全**
# （落到权限矩阵的 `ask`，由人/无人值守策略处置），而不是"看着像安全就放行"。

# 命令链 / 命令替换 / 换行：任一命中即不判安全。
#   `&` 与 `\n` 是曾经漏掉的两个分隔符 —— `ls & rm -rf x`、`ls\nrm -rf x`
#   都曾因"首 token 是 ls"被判成安全只读。
#   `(?<!>)&` 是刻意留的口子：`2>&1`（`&` 紧跟在 `>` 后）是 fd 重定向而非命令
#   分隔符，不该误伤；而 `ls &> f`（bash 的"重定向 stdout+stderr"）会被下面的
#   重定向检查拦下。
#   `|` 例外：下方对每段做完整校验，以支持 `echo hi | grep hi`。
_CHAIN_RE = re.compile(r"&&|;|(?<!>)&|`|\$\(|\n|\r")

# 路径逃逸：绝对路径 / 家目录 / 盘符 / UNC / `..` 组件。
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_DOTDOT_RE = re.compile(r"(^|[\\/])\.\.([\\/]|$)")

# 重定向目标里"写了也无害"的白名单（丢弃输出）。
_NULL_TARGETS = frozenset({"/dev/null", "nul", "nul:"})

# 把"只读命令"变成写/执行原语的选项，按命令分别列。
_UNSAFE_FLAGS: dict[str, frozenset[str]] = {
    "find": frozenset(
        {
            "-delete",
            "-exec",
            "-execdir",
            "-ok",
            "-okdir",
            "-fprint",
            "-fprint0",
            "-fprintf",
            "-fls",
        }
    ),
    "sort": frozenset({"-o", "--output"}),
}


def _escapes_workspace(tok: str) -> bool:
    """token 是否指向工作区之外的路径。

    判据是**文本形态**，不解析真实路径（本层不知道 cwd）：
    绝对路径 / `~` / 盘符 / UNC / 含 `..` 组件 → 视为逃逸 → 不判安全。
    代价是 `cat /etc/hostname` 这类无害绝对路径也会开始问人 —— 刻意的取舍。
    """
    t = tok.strip("'\"")
    if not t:
        return False
    if "=" in t:  # `--file=/etc/passwd` 这类
        t = t.split("=", 1)[1].strip("'\"")
        if not t:
            return False
    if t.startswith(("~", "/", "\\\\")):
        return True
    if _DRIVE_RE.match(t):
        return True
    return bool(_DOTDOT_RE.search(t))


def _redirect_is_unsafe(op: str, target: str) -> bool:
    """重定向是否让命令带上副作用（写文件 / 越界读）。"""
    t = target.strip("'\"")
    if t.lower() in _NULL_TARGETS or (t.startswith("&") and t[1:].isdigit()):
        return False
    if op in (">", ">>"):
        return True  # 任何真实文件写入都不算"只读"
    return _escapes_workspace(t)  # `<`：只挡越界读


def is_safe_command(command: str) -> bool:
    """判断命令是否为安全只读操作。

    解析 pipes 和重定向，逐段判断每段命令是否安全。
    安全 = 所有段都是安全命令、无链式分隔符、无越界路径、无写重定向、
    且不含把命令变成写/执行原语的选项。
    """
    if not command or not command.strip():
        return False

    cmd_stripped = command.strip()

    # 1. 先检查整体危险模式（避免分段绕过）
    if re.search(r">\s*/dev/(sd|nvme|loop|md|dm)", cmd_stripped):
        return False
    if re.search(r"<\s*/dev/(sd|nvme|loop|md|dm)", cmd_stripped):
        return False

    # 1b. 链式/命令替换/换行：安全前缀不得掩盖后续危险命令。
    if _CHAIN_RE.search(cmd_stripped):
        return False

    # 2. 逐段校验（含重定向：`> file` 是写入，`< 越界路径` 是越界读）
    for seg in _split_pipes(cmd_stripped):
        cmd_body, redirects = _split_redirects(seg.strip())
        for op, target in redirects:
            if _redirect_is_unsafe(op, target):
                return False
        if not cmd_body.strip():
            continue

        if not _is_safe_segment(cmd_body.strip()):
            return False

    return True


def _split_pipes(command: str) -> list[str]:
    """将管道命令拆分为独立段。"""
    # 简单按 | 拆分（不处理引号内的 |）
    segments: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    for ch in command:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "|" and not in_single and not in_double:
            segments.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        segments.append("".join(current))
    return segments if segments else [command]


def _split_redirects(command: str) -> tuple[str, list[tuple[str, str]]]:
    """分离命令体和重定向。返回 (cmd_body, [(op, target)])。

    与旧实现的关键差别：**重定向不再被丢弃**。旧实现只返回命令体、
    把重定向段直接扔掉，于是 `echo x > ~/.bashrc` 只校验到 `echo x` —— 判"安全只读"。
    现在把 (操作符, 目标) 一并交给 `_redirect_is_unsafe` 判。
    """
    parts = re.split(r"\s*(>>|>|<)\s*(\S+)", command.strip())
    if not parts:
        return "", []
    body = parts[0].strip()
    redirects: list[tuple[str, str]] = []
    # re.split 带捕获组 → [body, op, target, op, target, ...]
    for i in range(1, len(parts) - 1, 2):
        op, target = parts[i], parts[i + 1]
        if op and target:
            redirects.append((op, target))
    return body, redirects


def _is_safe_segment(seg: str) -> bool:
    """判断单个命令段是否安全。"""
    parts = seg.split()
    if not parts:
        return False

    base_cmd = parts[0]

    # 写/执行类选项：把"只读命令"变成副作用命令（`find . -delete`、`sort -o f`）。
    unsafe_flags = _UNSAFE_FLAGS.get(base_cmd)
    if unsafe_flags and any(p.split("=", 1)[0] in unsafe_flags for p in parts[1:]):
        return False

    # 路径逃逸：任何参数指向工作区之外 → 不判安全（`cat ~/.ssh/id_rsa`、
    # `find / -name id_rsa`、`grep -r x /etc` 都从这里被拦下）。
    if any(_escapes_workspace(p) for p in parts[1:]):
        return False

    # VCS 命令特殊处理
    if base_cmd == "git" and len(parts) >= 2:
        # 先试**两词**形式 —— 它存在的意义是区分「`stash list`（安全）」与
        # 「`stash`（危险）」。两词不中再回退到**单词**子命令。
        #
        # ⚠️ 修的是一个 fail-open 漏洞：旧实现**只看两词**，
        #    `git commit -m x` 取到的是 `'commit -m'` —— 两个集合都不匹配，
        #    于是落到下面那条"未知子命令 = 安全"的分支。后果是
        #    `git reset --hard` / `git clean -fdx` / `git push --force` /
        #    `git checkout -- .` **全部被当成只读命令直接放行**，
        #    连 `deny_all` 档都绕得过去（审批、审查者、置信度闸门全被跳过，
        #    因为权限层在这里就说了 allow）。
        #    注意 `commit` / `reset` / `push` 本来就在危险集合里 ——
        #    集合没错，**取词取错了**。
        two = " ".join(parts[1:3]) if len(parts) >= 3 else parts[1]
        if two in _SAFE_GIT_SUBCOMMANDS:
            return True
        if two in _DESTRUCTIVE_GIT_SUBCOMMANDS:
            return False

        sub = parts[1]
        if sub in _SAFE_GIT_SUBCOMMANDS:
            return True
        if sub in _DESTRUCTIVE_GIT_SUBCOMMANDS:
            return False

        # 未知 git 子命令 → **fail-closed**（不再"按 git 本身判断 = 安全"）。
        # 交给权限矩阵判，落到 `ask`。代价是 `git grep` 这类无害子命令也会问人 ——
        # 这是刻意选的：**不确定就问，比不确定就放行安全**。
        return False

    # 完整命令匹配（如 "python --version"）
    full_cmd = " ".join(parts[:2]) if len(parts) >= 2 else parts[0]
    if full_cmd in _SAFE_COMMANDS:
        return True

    # 基础命令匹配
    if base_cmd in _SAFE_COMMANDS:
        return True

    return False


# ── 内容提取 ────────────────────────────────────────────────


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    """从工具参数中提取用于规则匹配的内容文本。

    - BashTool: 提取 command（**完整命令，不截断**）
    - ReadFile / EditFile / WriteFile / Glob / Grep: 提取 file_path 或 path 参数
    - 其他: 尝试拼接参数摘要
    """
    if not arguments:
        return ""

    # 命令类工具
    if "command" in arguments:
        # ⚠️ 旧实现取 `split("\n")[0]`（只第一行）。多行命令 `ls\nrm -rf ~` 因此
        # 被安全判定/危险黑名单/规则引擎只看到 `ls` —— 第二行起完全不检。
        # 这里返回**完整命令**；多行本身在 `is_safe_command` 里已被判非安全。
        return str(arguments["command"]).strip()

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


#: 可能携带文件系统目标的参数名（按优先级）。
_PATH_ARG_KEYS = ("file_path", "path")


def extract_paths(arguments: dict[str, Any]) -> list[str]:
    """提取工具调用真正要触碰的文件系统路径（供路径沙箱检查）。

    与 `extract_content` 的分工：`content` 是给规则匹配/展示用的**文本**，
    `paths` 只给沙箱用，因此**只认真正的路径参数**：

    - `Glob` / `Grep` 的 `pattern` **不是路径**（没有 `path` 时根就是 cwd，天然在内），
      旧实现把 pattern 当路径送去沙箱，属于误伤来源。
    - 缺失或非字符串 → 空列表（沙箱不介入）。
    """
    if not arguments:
        return []
    for key in _PATH_ARG_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return [value]
    return []


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

    def add_session_rule(
        self, tool_pattern: str, content_pattern: str, action: RuleAction
    ) -> None:
        """添加会话级临时规则（HITL 选择"本会话允许"后）。"""
        self.session_rules.append(
            Rule(
                tool_pattern=tool_pattern,
                content_pattern=content_pattern,
                action=action,
                source="session",
            )
        )

    def load_project_rules(self, cwd: str | Path) -> None:
        """加载项目级 .codeforge/settings.json 的 rules。"""
        path = Path(cwd) / ".codeforge" / "settings.json"
        self.project_rules = _load_rules_from_file(path, "project")

    def load_user_rules(self) -> None:
        """加载用户全局 ~/.codeforge/settings.json 的 rules。"""
        path = Path.home() / ".codeforge" / "settings.json"
        self.user_rules = _load_rules_from_file(path, "user")

    def load_rules(self, cwd: str | Path) -> None:
        """一次性加载项目级 + 用户级规则。

        这是**唯一**该被会话装配调用的入口 —— 此前 `load_project_rules` /
        `load_user_rules` 定义了却无人调用，于是 Layer 3 永远匹配空规则集，
        用户在 `settings.json` 里写的 deny 规则静默失效。
        """
        self.load_project_rules(cwd)
        self.load_user_rules()

    def save_project_rule(
        self,
        cwd: str | Path,
        tool_pattern: str,
        content_pattern: str,
        action: RuleAction,
    ) -> bool:
        """把一条规则持久化到项目级 `.codeforge/settings.json`。

        对应 HITL 的「允许并保存」选项 —— 此前该选项只写进会话内存，
        用户以为记下了、重启就没了。

        Returns:
            True 写入成功；False 失败（不抛异常，调用方据此提示）。
        """
        path = Path(cwd) / ".codeforge" / "settings.json"
        try:
            data: dict[str, Any] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (json.JSONDecodeError, OSError):
                    # 文件损坏：不覆盖用户既有内容，直接放弃写入
                    return False
            permissions = data.setdefault("permissions", {})
            if not isinstance(permissions, dict):
                return False
            rules = permissions.setdefault("rules", [])
            if not isinstance(rules, list):
                return False
            rules.append(
                {
                    "tool": tool_pattern,
                    "pattern": content_pattern,
                    "action": action,
                }
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return False
        self.project_rules = _load_rules_from_file(path, "project")
        return True

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
            rules.append(
                Rule(
                    tool_pattern=entry.get("tool", "*"),
                    content_pattern=entry.get("pattern", "*"),
                    action=entry.get("action", "ask"),
                    source=source,
                )
            )
        except (KeyError, TypeError):
            continue
    return rules
