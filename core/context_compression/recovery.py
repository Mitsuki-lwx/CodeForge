"""恢复段构造。

摘要后的三段恢复内容：
1. 最近读过的文件快照
2. 当前可用工具列表
3. 边界提示消息

纯函数，不修改外部状态。
调用方必须在入口拍好 RecoveryState.snapshot() 快照传入。
"""

from __future__ import annotations

import json
from typing import Any

from core.context_compression.const import (
    ESTIMATE_CHARS_PER_TOKEN,
    RECOVERY_FILE_LIMIT,
    RECOVERY_TOKENS_PER_FILE,
)
from core.context_compression.state import FileReadRecord

# ── 边界提示 ─────────────────────────────────────────────────────

BOUNDARY_NOTICE: str = (
    "[边界消息] 上面是之前对话的摘要。如果需要文件的具体内容，"
    "请用 ReadFile 重新读取，不要根据摘要猜测代码细节。"
)


# ── 单文件块渲染 ─────────────────────────────────────────────────


def render_file_block(rec: FileReadRecord) -> str:
    """渲染单个文件快照。

    超过 RECOVERY_TOKENS_PER_FILE token 时保留头部、截掉尾部，
    并追加 (content truncated) 标记。

    Args:
        rec: 文件读取记录。

    Returns:
        格式化的文件快照字符串。
    """
    char_limit = int(RECOVERY_TOKENS_PER_FILE * ESTIMATE_CHARS_PER_TOKEN)
    content = rec.content

    if len(content) > char_limit:
        content = content[:char_limit] + "\n(content truncated)"

    return f"### {rec.path}\n[read at] {rec.timestamp.isoformat()}\n{content}\n"


# ── 工具块渲染 ───────────────────────────────────────────────────


def render_tools_block(defs: list[dict[str, Any]]) -> str:
    """渲染工具列表。

    每行一个工具：名 + 描述 + input_schema 紧凑 JSON。

    Args:
        defs: 工具定义列表（name, description, input_schema）。

    Returns:
        格式化的工具列表字符串。
    """
    if not defs:
        return "(无可用工具)\n"

    lines = []
    for t in defs:
        name = t.get("name", "unknown")
        desc = t.get("description", "")
        schema = t.get("input_schema", {})
        schema_str = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
        lines.append(f"- {name}: {desc}")
        lines.append(f"  schema: {schema_str}")
    return "\n".join(lines)


# ── 三段拼接 ─────────────────────────────────────────────────────


def build_recovery_attachment(
    snapshot: list[FileReadRecord],
    tool_defs: list[dict[str, Any]],
) -> str:
    """构造摘要后的恢复三段内容。

    三段：
      1. 最近读过的文件快照（取前 RECOVERY_FILE_LIMIT 个，时间戳倒序）
      2. 当前可用工具列表（来自 tool_defs，与 Request.tools 同源）
      3. 边界提示消息

    Args:
        snapshot: RecoveryState.snapshot() 的快照（已按时间戳倒序）。
        tool_defs: 工具定义列表（与发送给 LLM 的 tools 同一引用）。

    Returns:
        纯文本恢复段（不包含摘要本身）。
    """
    import io

    buf = io.StringIO()

    # ── 第一段：最近读过的文件 ──
    buf.write("## 最近读过的文件\n")
    recent = snapshot[:RECOVERY_FILE_LIMIT]
    if not recent:
        buf.write("(无)\n")
    else:
        for rec in recent:
            buf.write(render_file_block(rec))
            buf.write("\n")

    # ── 第二段：当前可用工具 ──
    buf.write("## 当前可用工具\n")
    buf.write(render_tools_block(tool_defs))
    buf.write("\n")

    # ── 第三段：边界提示 ──
    buf.write("## 边界提示\n")
    buf.write(BOUNDARY_NOTICE)
    buf.write("\n")

    return buf.getvalue()
