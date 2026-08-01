"""项目指令文件发现与加载。

按优先级扫描三处 CODEFORGE.md：
  ① <project_root>/CODEFORGE.md              （项目级，最高优先级）
  ② <project_root>/.codeforge/CODEFORGE.md    （项目配置级）
  ③ ~/.codeforge/CODEFORGE.md                （用户级，最低优先级）

文件缺失静默跳过；每个文件携带其「根边界」，供 @include 沙箱校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 用户级指令的沙箱根：~/.codeforge/
USER_INSTRUCTION_ROOT_NAME = ".codeforge"


@dataclass(frozen=True)
class InstructionFile:
    """一个指令文件及其沙箱根。"""

    path: Path
    root: Path  # 沙箱根：项目级=项目根，用户级=~/.codeforge/


def discover_instruction_files(workspace: str | Path) -> list[InstructionFile]:
    """按优先级返回存在的指令文件（缺失静默跳过）。

    Returns:
        按 ①项目根 → ②项目配置级 → ③用户级 排序的 InstructionFile 列表。
    """
    ws = Path(workspace).resolve()
    user_root = Path.home() / USER_INSTRUCTION_ROOT_NAME
    candidates = [
        InstructionFile(path=ws / "CODEFORGE.md", root=ws),
        InstructionFile(path=ws / ".codeforge" / "CODEFORGE.md", root=ws),
        InstructionFile(path=user_root / "CODEFORGE.md", root=user_root),
    ]
    return [c for c in candidates if c.path.is_file()]
