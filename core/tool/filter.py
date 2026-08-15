"""工具过滤多层防线。

提供子 Agent 工具集过滤的常量与函数，按五层顺序应用：
  ① 全局禁止列表 → ② 自定义禁止列表 → ③ 后台白名单 →
  ④ 角色黑名单 → ⑤ 角色白名单
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── 常量 ──────────────────────────────────────────────────────────

# 任何子 Agent 永远不能用的工具名列表。
# 最小列表：Agent（防止无限嵌套）。
ALL_AGENT_DISALLOWED_TOOLS: list[str] = ["Agent"]

# 自定义（非内置来源）Agent 比内置 Agent 多禁用的工具。本期为空。
CUSTOM_AGENT_DISALLOWED_TOOLS: list[str] = []

# 后台 Agent 工具白名单。
# 只含基础读写工具；不含 Agent / TaskStop / SendMessage / TaskList / TaskGet
# 等元工具。MCP / Skill 工具通过 is_mcp_or_skill 另行判定。
ASYNC_AGENT_ALLOWED_TOOLS: list[str] = [
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    "load_skill",
    "install_skill",
]

# ── 辅助判定 ──────────────────────────────────────────────────────


def is_mcp_or_skill(name: str) -> bool:
    """判定工具名是否属于 MCP 工具或 Skill 工具。

    MCP 工具以 "mcp__" 为前缀；Skill 工具暂按命名约定识别。
    """
    return name.startswith("mcp__")


# ── 参数与过滤函数 ────────────────────────────────────────────────


@dataclass
class FilterParams:
    """工具过滤参数。"""

    all: list[str]  # registry 的全部工具名（保持注册顺序）
    source: int = 0  # Source 枚举的整数值（0=BUILTIN, 1=USER, 2=PROJECT, 3=PLUGIN）
    background: bool = False
    allowed: list[str] = field(default_factory=list)  # Agent 定义的 tools 白名单
    disallowed: list[str] = field(default_factory=list)  # Agent 定义的 disallowedTools


def apply_agent_tool_filter(p: FilterParams) -> list[str]:
    """按各层顺序过滤工具名列表。

    Args:
        p: FilterParams，含全部工具名、来源、后台标记、角色白/黑名单、team 标记。

    Returns:
        过滤后的工具名列表（保持原始顺序）。
    """
    result = list(p.all)

    # ① 全局禁止
    result = [t for t in result if t not in ALL_AGENT_DISALLOWED_TOOLS]

    # ② 自定义来源额外禁止
    if p.source >= 1:  # 非 BUILTIN
        result = [t for t in result if t not in CUSTOM_AGENT_DISALLOWED_TOOLS]

    # ③ 后台白名单
    if p.background:
        result = [
            t
            for t in result
            if t in ASYNC_AGENT_ALLOWED_TOOLS or is_mcp_or_skill(t)
        ]

    # ④ 角色黑名单
    if p.disallowed:
        result = [t for t in result if t not in p.disallowed]

    # ⑤ 角色白名单（空 = 不收窄）
    if p.allowed:
        result = [t for t in result if t in p.allowed]

    return result
