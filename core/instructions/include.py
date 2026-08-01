"""@include 模块化引用展开器。

语法：独占一行 `@include <relative_path>`，路径相对当前文件所在目录解析。
限制：嵌套深度 ≤ 5、环路检测、路径不能越出所属沙箱根、二进制文件跳过。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.instructions.discovery import discover_instruction_files

logger = logging.getLogger(__name__)

# 最大嵌套深度：CODEFORGE.md 算第 1 层，被 include 的文件依次 +1，超过则跳过
MAX_INCLUDE_DEPTH = 5

_INCLUDE_RE = re.compile(r"^\s*@include\s+(\S+)\s*$")

_WARN_DEPTH = "<!-- @include 超过最大嵌套深度，已跳过: {path} -->"
_WARN_CYCLE = "<!-- @include 检测到环路，已跳过: {path} -->"
_WARN_ESCAPE = "<!-- @include 路径超出允许范围，已跳过: {path} -->"
_WARN_BINARY = "<!-- @include 文件为二进制，已跳过: {path} -->"


def _is_binary(path: Path) -> bool:
    """前 512 字节含 \\x00 判定为二进制。"""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return True
    return b"\x00" in head


def _expand_file(path: Path, root: Path, visited: set[Path], depth: int) -> str:
    """递归展开单个文件，返回完整文本。

    Args:
        path: 当前文件绝对路径。
        root: 当前文件的沙箱根。
        visited: 本展开链上已加载的绝对路径集合（防环）。
        depth: 当前文件所处的嵌套层（CODEFORGE.md 为 1）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning("指令文件读取失败，跳过: %s", path)
        return ""

    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = _INCLUDE_RE.match(line)
        if not m:
            out.append(line)
            continue

        inc = m.group(1)
        candidate = (path.parent / inc).resolve()

        if depth + 1 > MAX_INCLUDE_DEPTH:
            warn = _WARN_DEPTH.format(path=candidate)
            out.append(warn + "\n")
            logger.warning(warn)
            continue
        if candidate in visited:
            warn = _WARN_CYCLE.format(path=candidate)
            out.append(warn + "\n")
            logger.warning(warn)
            continue
        if not candidate.is_relative_to(root.resolve()):
            warn = _WARN_ESCAPE.format(path=candidate)
            out.append(warn + "\n")
            logger.warning(warn)
            continue
        if not candidate.is_file():
            logger.warning("@include 文件不存在，静默跳过: %s", candidate)
            continue
        if _is_binary(candidate):
            warn = _WARN_BINARY.format(path=candidate)
            out.append(warn + "\n")
            logger.warning(warn)
            continue

        visited.add(candidate)
        out.append(_expand_file(candidate, root, visited, depth + 1))

    return "".join(out)


def load_instructions(workspace: str | Path) -> str:
    """发现三处指令文件并递归展开，按优先级拼接。

    高优先级在前，各层之间以空行分隔；每层带来源标注 `## 来自 <路径>`。
    缺失文件静默跳过，不影响其他层。

    Args:
        workspace: 项目根目录。

    Returns:
        合并后的指令文本（无文件时为空字符串）。
    """
    files = discover_instruction_files(workspace)
    blocks: list[str] = []
    for f in files:
        visited: set[Path] = {f.path.resolve()}
        content = _expand_file(f.path, f.root, visited, 1)
        if content.strip():
            blocks.append(f"## 来自 {f.path}\n\n{content.rstrip()}")
    return "\n\n".join(blocks)
