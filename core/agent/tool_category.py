"""工具 `category()` → 权限类别（read / write / command）归一化。

工具自述的 category 比权限系统需要的更细（file / code_search / shell /
task / skill / mcp …），权限系统只认三档，这里做收敛。

从 `core/agent/agent.py` 抽出：这是**纯函数**，不需要 Agent 的任何状态，
放在 Agent 里只会让那个文件更难读。
"""

from __future__ import annotations

from core.permissions.modes import ToolCategory

_READ_CATEGORIES: frozenset[str] = frozenset(
    {"read", "file_read", "search", "code_search"}
)
_WRITE_CATEGORIES: frozenset[str] = frozenset({"write", "file_write", "edit", "plan"})
_COMMAND_CATEGORIES: frozenset[str] = frozenset({"shell", "command", "bash"})


def normalize_tool_category(category: str, is_read_only: bool) -> ToolCategory:
    """把 `tool.category()` 归一化为权限类别。

    `file` 是读写工具**共用**的 category（`read_file` / `write_file` /
    `edit_file` 都返回 `"file"`），因此必须靠 `is_read_only` 区分。若把
    `"file"` 一律当成 command 兜底，写文件会被误判成命令执行，导致：

    - `acceptEdits` 模式对写文件失效（矩阵是 write→allow，但实际走 command→ask）；
    - 写操作与命令执行绑在同一分类上，无法单独设策略。

    未识别的 category 退回原有兜底：只读→read，否则→command。
    """
    if category in _READ_CATEGORIES:
        return "read"
    if category in _WRITE_CATEGORIES:
        return "write"
    if category in _COMMAND_CATEGORIES:
        return "command"
    if category == "file":
        return "read" if is_read_only else "write"
    return "read" if is_read_only else "command"


__all__ = ["normalize_tool_category"]
