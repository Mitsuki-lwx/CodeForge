"""/show —— 主会话的展开详情（补"当场极压、按需无入口"的窟窿）。

```
/show                        轮次概览（每轮：问了什么 / 几个工具 / 耗时）
/show <轮次>                  展开某一轮：用户输入、工具调用与参数、完整结果、模型回复
/show --tail N               概览只看最近 N 轮
/show --full                 不截断（默认单条 1000 字符，与 /agents show 同常量）
/show --tools                只看工具调用与结果
/show --cost                 本会话 token / 工具调用 / 耗时 + 落盘脱敏状态
```

**为什么是命令式而不是 `Ctrl+O` 式就地展开**：`docs/spec_show_redact.md` §4.1
—— 阻塞式读键会冻住事件循环（已实测），且主对话输出已滚出屏幕，事后重放更实用。

脱敏边界（`docs/spec_show_redact.md` §4.5）：本命令**显示原文** ——
脱敏只作用于落盘。用户自己看的东西遮了就没法排查。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.commands.ui import UI

_USAGE = (
    "Usage: /show [<轮次>] [--tail N] [--full] [--tools] [--cost]"
)


async def handle_show(ui: UI, args: str = "") -> None:
    """分发 `/show`。以 `x ` 开头的行按错误渲染（与 `/agents` 同一约定）。"""
    for line in ui.show_lines(args or ""):
        if line.startswith("x "):
            ui.error(line[2:])
        else:
            ui.print_markup(line)


__all__ = ["handle_show"]
