from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Unified result for all tool executions."""

    success: bool
    data: Any = None
    error: str | None = None
    meta: dict = field(default_factory=dict)
    # 图像附件（可选）：`[{"media_type": "image/png", "data": "<base64>"}]`。
    #
    # ⚠️ 刻意**不放进 `meta`**：`meta` 会进 trace / 日志 / span 属性，
    # 而 base64 图像是几百 KB 的二进制 —— 塞进去会把审计文件撑爆，
    # 也可能把画面里的敏感内容写进日志。走独立字段，由执行层决定要不要回灌。
    images: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.success

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAIL"
        detail = f" data={self.data!r}" if self.data is not None else ""
        detail += f" error={self.error!r}" if self.error else ""
        detail += f" meta={self.meta}" if self.meta else ""
        if self.images:
            detail += f" images={len(self.images)}"
        return f"<ToolResult {status}{detail}>"
