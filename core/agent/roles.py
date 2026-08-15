"""Agent 角色定义 —— 数据类 + YAML frontmatter 解析。

从 Markdown + YAML frontmatter 文件解析子 Agent 角色定义。
内置定义解析失败 raise（代码 bug），
用户/项目级定义解析失败 stderr 警告并返回 None。
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from enum import IntEnum

import yaml

from core.permissions.modes import PermissionMode

logger = logging.getLogger(__name__)

# name 允许大小写字母开头，后续可含字母/数字/连字符/下划线，长度 1-32
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-_]{0,31}$")

# 有效的 model 值
_VALID_MODELS = frozenset({"haiku", "sonnet", "opus", "inherit"})

# 有效的 permissionMode → PermissionMode 映射
_PERMISSION_MODE_MAP: dict[str, PermissionMode] = {
    "default": PermissionMode.DEFAULT,
    "acceptedits": PermissionMode.ACCEPT_EDITS,
    "plan": PermissionMode.PLAN,
    "bypasspermissions": PermissionMode.BYPASS,
}


class Source(IntEnum):
    """角色定义来源，数值越大优先级越高。"""

    BUILTIN = 0
    USER = 1
    PROJECT = 2
    PLUGIN = 3  # 占位，本期不实现

    def __str__(self) -> str:
        return {0: "builtin", 1: "user", 2: "project", 3: "plugin"}.get(
            int(self), "unknown"
        )


@dataclass
class AgentRole:
    """一个 Agent 角色的完整定义。

    从 Markdown + YAML frontmatter 解析而成。
    """

    name: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)          # frontmatter.tools 白名单
    disallowed_tools: list[str] = field(default_factory=list)  # frontmatter.disallowedTools
    model: str = "inherit"                                    # haiku / sonnet / opus / inherit
    max_turns: int = 0                                        # 0 = 沿用全局默认
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    dont_ask: bool = False                                    # 子 Agent 专属：自动批准 Ask 决策
    background: bool = False                                  # 强制后台
    isolation: bool = False                                   # 需要 Worktree 文件隔离
    system_prompt: str = ""                                   # Markdown body
    file_path: str = ""                                       # 来源文件绝对路径
    source: Source = Source.BUILTIN

    def is_fork(self) -> bool:
        """是否为 Fork 路径的虚拟角色定义。"""
        return self.name == "__fork__"


# ── 解析入口 ──────────────────────────────────────────────────────


def parse_role_bytes(
    data: bytes,
    file_path: str,
    source: Source,
) -> AgentRole | None:
    """从字节数据解析角色定义。

    Args:
        data: Markdown 文件原始字节。
        file_path: 来源文件路径（用于错误信息与调试）。
        source: 定义来源。

    Returns:
        解析成功的 AgentRole；解析失败时：
        - 内置定义 → raise ValueError
        - 用户/项目定义 → stderr 警告 + 返回 None
    """
    raw = data.decode("utf-8")
    fm_dict, body = _split_frontmatter(raw, file_path)
    if fm_dict is None:
        if source == Source.BUILTIN:
            raise ValueError(f"builtin role {file_path}: failed to parse frontmatter")
        return None

    role = _build_role(fm_dict, body, file_path, source)
    if role is None:
        if source == Source.BUILTIN:
            raise ValueError(f"builtin role {file_path}: validation failed")
        return None
    return role


def parse_role_file(
    path: str,
    source: Source,
) -> AgentRole | None:
    """解析单个 .md 角色文件。

    Args:
        path: .md 文件绝对路径。
        source: 定义来源。

    Returns:
        AgentRole 或 None（解析失败时）。
    """
    from pathlib import Path

    return parse_role_bytes(Path(path).read_bytes(), path, source)


# ── 内部函数 ──────────────────────────────────────────────────────


def _split_frontmatter(raw: str, file_path: str) -> tuple[dict | None, str]:
    """分离 YAML frontmatter 与正文。

    Args:
        raw: Markdown 原始文本。
        file_path: 来源路径（仅用于错误信息）。

    Returns:
        (frontmatter_dict, body)。解析失败时返回 (None, "")。
    """
    stripped = raw.lstrip("﻿")  # BOM
    if not stripped.startswith("---"):
        print(
            f"subagent {file_path}: missing opening '---' frontmatter delimiter, skipped",
            file=sys.stderr,
        )
        return None, ""

    end_idx = stripped.find("---", 3)
    if end_idx == -1:
        print(
            f"subagent {file_path}: unclosed frontmatter, skipped",
            file=sys.stderr,
        )
        return None, ""

    yaml_str = stripped[3:end_idx].strip()
    body = stripped[end_idx + 3:].strip()

    if not yaml_str:
        print(
            f"subagent {file_path}: empty frontmatter, skipped",
            file=sys.stderr,
        )
        return None, ""

    try:
        fm_dict = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        print(
            f"subagent {file_path}: invalid YAML in frontmatter: {e}, skipped",
            file=sys.stderr,
        )
        return None, ""

    if not isinstance(fm_dict, dict):
        print(
            f"subagent {file_path}: frontmatter must be a YAML mapping, skipped",
            file=sys.stderr,
        )
        return None, ""

    return fm_dict, body


def _build_role(
    fm: dict,
    body: str,
    file_path: str,
    source: Source,
) -> AgentRole | None:
    """从 frontmatter 字典构建 AgentRole，校验必填字段与值域。

    Returns:
        AgentRole 或 None（校验失败时）。
    """

    def _warn(msg: str) -> None:
        print(f"subagent {file_path}: {msg}, skipped", file=sys.stderr)

    # --- name (必填) ---
    name = fm.get("name")
    if not name or not isinstance(name, str):
        _warn("missing or invalid 'name' field")
        return None
    name = name.strip()
    if not _NAME_RE.match(name):
        _warn(
            f"invalid name '{name}': must match {_NAME_RE.pattern}"
        )
        return None

    # --- description (必填) ---
    description = fm.get("description")
    if not description or not isinstance(description, str):
        _warn("missing or invalid 'description' field")
        return None
    description = description.strip()

    # --- tools (可选) ---
    tools = fm.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        _warn("'tools' must be a list")
        return None
    for t in tools:
        if not isinstance(t, str):
            _warn(f"each entry in 'tools' must be a string, got {type(t).__name__}")
            return None

    # --- disallowedTools (可选) ---
    disallowed_tools = fm.get("disallowedTools", [])
    if disallowed_tools is None:
        disallowed_tools = []
    if not isinstance(disallowed_tools, list):
        _warn("'disallowedTools' must be a list")
        return None
    for t in disallowed_tools:
        if not isinstance(t, str):
            _warn(
                f"each entry in 'disallowedTools' must be a string, got {type(t).__name__}"
            )
            return None

    # --- model (可选) ---
    model = fm.get("model", "inherit")
    if model is None:
        model = "inherit"
    if not isinstance(model, str):
        _warn("'model' must be a string")
        return None
    model = model.strip().lower()
    if model not in _VALID_MODELS:
        print(
            f"subagent {file_path}: unknown model '{model}', defaulting to 'inherit'",
            file=sys.stderr,
        )
        model = "inherit"

    # --- maxTurns (可选) ---
    max_turns = fm.get("maxTurns", 0)
    if max_turns is None:
        max_turns = 0
    if not isinstance(max_turns, int):
        print(
            f"subagent {file_path}: 'maxTurns' must be an integer, defaulting to 0",
            file=sys.stderr,
        )
        max_turns = 0

    # --- permissionMode (可选) ---
    permission_mode_str = fm.get("permissionMode", "default")
    if permission_mode_str is None:
        permission_mode_str = "default"
    if not isinstance(permission_mode_str, str):
        print(
            f"subagent {file_path}: 'permissionMode' must be a string, defaulting to 'default'",
            file=sys.stderr,
        )
        permission_mode_str = "default"

    permission_mode_str = permission_mode_str.strip().lower()
    dont_ask = False
    if permission_mode_str == "dontask":
        dont_ask = True
        permission_mode = PermissionMode.DEFAULT
    elif permission_mode_str in _PERMISSION_MODE_MAP:
        permission_mode = _PERMISSION_MODE_MAP[permission_mode_str]
    else:
        print(
            f"subagent {file_path}: unknown permissionMode '{permission_mode_str}', "
            f"defaulting to 'default'",
            file=sys.stderr,
        )
        permission_mode = PermissionMode.DEFAULT

    # --- background (可选) ---
    background = fm.get("background", False)
    if background is None:
        background = False
    if not isinstance(background, bool):
        print(
            f"subagent {file_path}: 'background' must be a bool, defaulting to false",
            file=sys.stderr,
        )
        background = False

    # --- isolation (可选，值须为 "worktree") ---
    isolation = False
    isolation_val = fm.get("isolation", "")
    if isolation_val:
        if not isinstance(isolation_val, str):
            print(
                f"subagent {file_path}: 'isolation' must be a string, defaulting to false",
                file=sys.stderr,
            )
        else:
            stripped = isolation_val.strip().lower()
            if stripped == "worktree":
                isolation = True
            else:
                print(
                    f"subagent {file_path}: unknown isolation '{isolation_val}', "
                    f"defaulting to false (only 'worktree' is supported)",
                    file=sys.stderr,
                )

    return AgentRole(
        name=name,
        description=description,
        tools=tools,
        disallowed_tools=disallowed_tools,
        model=model,
        max_turns=max_turns,
        permission_mode=permission_mode,
        dont_ask=dont_ask,
        background=background,
        isolation=isolation,
        system_prompt=body,
        file_path=file_path,
        source=source,
    )
