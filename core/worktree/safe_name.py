"""Worktree 目录名校验与命名生成。

限制字符集和长度、拒绝 `.` 和 `..` 段、拒绝绝对路径段，防 LLM 输入触发路径遍历。
"""

from __future__ import annotations

import re
import secrets

# 目录名最大长度
NAME_MAX_LEN = 80

# 允许的字符：字母数字下划线连字符 + 斜杠（允许嵌套子目录）
SAFE_CHARS_RE = re.compile(r"^[a-zA-Z0-9_\-/]+$")

# 生成名前缀：SubAgent → "agent-"，工作流 → "wf_"
AGENT_PREFIX = "agent-"
WF_PREFIX = "wf_"


def is_safe_name(name: str) -> bool:
    """校验目录名是否安全。

    规则：
      - 非空且长度不超过 NAME_MAX_LEN
      - 仅含字母数字下划线连字符与斜杠（允许嵌套子目录）
      - 不以 / 开头（拒绝绝对路径段）
      - 分割成段后拒绝 '.' 和 '..' 段，拒绝空段（连续 /）
    """
    if not name or len(name) > NAME_MAX_LEN:
        return False
    if not SAFE_CHARS_RE.match(name):
        return False
    if name.startswith("/"):
        return False
    parts = name.split("/")
    for part in parts:
        if part in (".", "..", ""):
            return False
    return True


def generate_agent_name() -> str:
    """生成 SubAgent worktree 目录名。

    Returns:
        "agent-" + 7 位十六进制（"agent-" 是固定前缀，不属于 hex 部分）。
    """
    return AGENT_PREFIX + secrets.token_hex(4)[:7]


def generate_wf_name() -> str:
    """生成工作流 worktree 目录名。

    Returns:
        "wf_" + 固定格式十六进制。
    """
    return WF_PREFIX + secrets.token_hex(4)


def is_generated_name(name: str) -> bool:
    """判断目录名是否由系统生成（可自动清理）。

    用户通过 /worktree create 创建的自定义名不匹配这些前缀，
    永远不会被自动清理。

    Args:
        name: 目录名。

    Returns:
        True 如果以 agent- 或 wf_ 开头。
    """
    return name.startswith((AGENT_PREFIX, WF_PREFIX))
