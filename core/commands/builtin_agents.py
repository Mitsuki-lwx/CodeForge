"""/agents —— 后台任务 / 队友的列表、下钻与干预。

人的入口（不是模型的入口 —— 模型已有 `TaskList/TaskGet/TaskStop/SendMessage` 四个工具）：

```
/agents [list|all]                    列出后台任务（默认：运行中 + 最近 5 个已结束）
/agents show <序号|id|名字> [--tail N] [--full]
/agents stop <序号|id|名字>            单独停掉一个
/agents tell <序号|id|名字> <消息>      对它说话（跑着→排队续派；已停→立即续派）
```

**形态说明**：刻意不做方向键整屏面板 —— 阻塞式读键会冻住事件循环，
面板开着时被旁观的后台队友停止推进（实验见 spec §4.2）；终端本身就是滚动器。
见 `docs/spec_teammate_inspect.md` §4。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.commands.ui import UI

_SUBCOMMANDS = (
    "list / all / show <序号|id|名字> [--tail N] [--full] / stop <…> / tell <…> <消息>"
)


async def handle_agents(ui: UI, args: str = "") -> None:
    """分发 /agents <subcommand>。"""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""

    if sub in ("list", "all"):
        _print(ui, ui.agent_list_lines(show_all=(sub == "all")))
    elif sub == "show":
        sel, tail, full = _parse_show(rest)
        if not sel:
            ui.error("Usage: /agents show <序号|id|名字> [--tail N] [--full]")
            return
        _print(ui, ui.agent_show_lines(sel, tail=tail, full=full))
    elif sub == "stop":
        sel = rest.strip()
        if not sel:
            ui.error("Usage: /agents stop <序号|id|名字>")
            return
        _say(ui, await ui.agent_stop(sel))
    elif sub == "tell":
        sel, _, message = rest.strip().partition(" ")
        if not sel or not message.strip():
            ui.error("Usage: /agents tell <序号|id|名字> <消息>")
            return
        _say(ui, await ui.agent_tell(sel, message.strip()))
    else:
        ui.error(f"Unknown subcommand: /agents {sub}. Available: {_SUBCOMMANDS}")


def _print(ui: UI, lines: list[str]) -> None:
    for line in lines:
        ui.print_markup(line)


def _say(ui: UI, text: str) -> None:
    """回显一句话：以 `x ` 开头视为错误，其余按普通输出（不套 dim）。"""
    if text.startswith("x "):
        ui.error(text[2:])
    else:
        ui.print_markup(text)


def _parse_show(args: str) -> tuple[str, int, bool]:
    """解析 `show` 的参数：`<sel> [--tail N] [--tail=N] [--full]`。

    选择器取**第一个非选项** token —— 其余位置无关（`--full 3` 也认）。
    """
    from core.task.view import DEFAULT_TAIL

    sel = ""
    tail = DEFAULT_TAIL
    full = False
    toks = args.split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok == "--full":
            full = True
        elif tok == "--tail":
            i += 1
            if i < len(toks):
                tail = _positive_int(toks[i], tail)
        elif tok.startswith("--tail="):
            tail = _positive_int(tok.split("=", 1)[1], tail)
        elif not sel:
            sel = tok
        i += 1
    return sel, tail, full


def _positive_int(raw: str, default: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default
