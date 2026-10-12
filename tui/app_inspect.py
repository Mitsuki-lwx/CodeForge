"""`CodeForgeApp` 的检视类命令 —— `/agents`、`/show`。

从 `tui/app.py` 抽出：这些方法只是把请求转给 `tui/agent_view.py` /
`tui/show_view.py` 的渲染函数，与 TUI 主循环无关。

用 Mixin：`self.` 不变、调用点零改动、行为逐字不变。
"""

from __future__ import annotations

from core.task.view import DEFAULT_TAIL
from tui import agent_view, show_view


class InspectionMixin:
    """`/agents` 与 `/show` 的取数入口。"""

    def agent_list_lines(self, show_all: bool = False) -> list[str]:
        return agent_view.list_lines(self, show_all=show_all)

    def agent_show_lines(
        self, sel: str, tail: int = DEFAULT_TAIL, full: bool = False
    ) -> list[str]:
        return agent_view.show_lines(self, sel, tail=tail, full=full)

    async def agent_stop(self, sel: str) -> str:
        return await agent_view.stop(self, sel)

    async def agent_tell(self, sel: str, message: str) -> str:
        return await agent_view.tell(self, sel, message)

    # ── 主会话展开详情（spec_show_redact）──
    # 实现在 tui/show_view.py（渲染在 core/task/view.py，纯函数）。

    def show_lines(self, args: str) -> list[str]:
        return show_view.render(self, args)
