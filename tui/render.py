"""TUI 的纯渲染件 —— 横幅 / 流式渲染 / 工具标题 / 压缩提示 / spinner。

从 `tui/app.py` 抽出的一簇**叶子函数与渲染类**：它们不参与会话编排，
只把事件变成终端文本。app.py 里剩下的都是编排（run / _run_async /
_consume_agent_stream / _consume_task_done），这一层混在里面会把两者都读不清。

`tui.app` 仍然 re-export 这些名字（`from tui.render import *` 之外的显式导入），
因此 `tui.app._StreamRenderer` 这类既有引用点不需要改。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console

from core.agent.events import CompactEvent, CompactPhase

if TYPE_CHECKING:
    from tui.app import CodeForgeApp


CAT_ASCII = r"""      /\_/\
     (o.o)
     > ^ <"""


def _detect_language(text: str) -> str:
    """检测文本主导语言。返回 'zh'、'en' 或 ''（无法判断）。"""
    chinese_chars = sum(1 for c in text if "一" <= c <= "鿿")
    english_chars = sum(1 for c in text if c.isascii() and c.isalpha())
    if chinese_chars == 0 and english_chars == 0:
        return ""
    return "zh" if chinese_chars > english_chars else "en"


_LANG_HINTS = {
    "zh": "\n\n[System: Reply in Chinese (中文)]",
    "en": "\n\n[System: Reply in English]",
}


def format_compact_notice(before: int, after: int) -> str:
    """压缩完成后的统一系统消息文案。"""
    return f"已压缩，token 从 {before} 降至 {after}"


def _command_key_bindings() -> KeyBindings:
    """回车行为：补全菜单打开且有高亮命令 → 直接执行；否则正常提交。"""

    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event):
        buf = event.current_buffer
        if (
            buf.complete_state is not None
            and buf.complete_state.current_completion is not None
        ):
            comp = buf.complete_state.current_completion
            buf.complete_state = None
            buf.apply_completion(comp)
            event.app.exit(result=buf.text)
        else:
            event.app.exit(result=buf.text)

    return kb


class _HeadlessSessionStub:
    """无头单任务模式的 placeholder，顶替 PromptSession。

    任务模式不进入交互循环、不调 `prompt_async`/补全，故无需真实 prompt_toolkit
    会话（后者在 GUI-less shell 会因无 Windows 屏幕缓冲区抛 NoConsoleScreenBufferError）。
    仅占位满足 `CodeForgeApp.session` 字段；任何误用 `prompt_async` 会给出明确错误。
    """

    async def prompt_async(self, *a, **k):
        raise RuntimeError("无头任务模式不提供交互输入")

    def __getattr__(self, name):
        raise AttributeError(f"_HeadlessSessionStub 无 {name}（任务模式仅占位）")


def _print_banner(console: Console, name: str, model: str) -> None:
    console.print(CAT_ASCII, style="cyan")
    console.print("[bold cyan]CodeForge[/] [white]0.1.0[/]")
    console.print(f"[dim]{name} - {model}[/]")
    console.print()


def _short_preview(content: str, max_len: int = 60) -> str:
    """结果/文本的单行短摘要（取首行、截断）。"""
    s = (content or "").strip()
    if not s:
        return ""
    first = s.splitlines()[0]
    if len(first) <= max_len:
        return first
    return first[: max_len - 3] + "..."


def _tool_title(name: str, input: dict) -> str:
    """工具调用的人类可读标题（参考 mewcode 的 _tool_title，避免原始 JSON 堆积）。"""
    n = name.lower()
    if n == "bash":
        cmd = input.get("command", "")
        short = cmd[:50] + "…" if len(cmd) > 50 else cmd
        return f"Bash: {short}" if short else "Bash"
    if n == "read_file":
        p = Path(input.get("file_path", "")).name if input.get("file_path") else ""
        return f"Read {p}" if p else "Read"
    if n == "write_file":
        p = Path(input.get("file_path", "")).name if input.get("file_path") else ""
        content = input.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"Write {p} ({lines} lines)" if p else "Write"
    if n == "edit_file":
        p = Path(input.get("file_path", "")).name if input.get("file_path") else ""
        return f"Edit {p}" if p else "Edit"
    if n == "glob":
        return f"Glob: {input.get('pattern', '')}"
    if n == "grep":
        return f"Grep: {input.get('pattern', '')}"
    if n == "exitplanmode":
        return "Submit plan"
    # MCP / 未知工具：挑一个主参做摘要
    for key in ("path", "file_path", "command", "pattern", "query", "url", "name"):
        val = input.get(key)
        if isinstance(val, str) and val:
            return f"{name}: {_short_preview(val, 50)}"
    return name


class _StreamRenderer:
    """流式输出渲染器：管理 spinner、思考块、回答分隔、工具步骤。

    思考内容以 dim 单独一块显示，正文前打「──── 回答 ────」分隔线，
    工具步骤用简洁标题（● / ✓ / ✗）呈现，三者清晰区分。
    """

    def __init__(self, console: Console, spinner_task) -> None:
        self.console = console
        self.spinner = spinner_task
        self.has_started = False  # 首个 Thinking/Text/Tool 到达，spinner 已停
        self.in_thinking = False  # 当前正处在思考块

    def _stop_spinner(self) -> None:
        if not self.has_started:
            self.has_started = True
            self.spinner.cancel()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def on_thinking(self, text: str) -> None:
        if not self.has_started:
            self.has_started = True
            self.spinner.cancel()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            sys.stdout.write("  \033[2m思考中\033[0m\n")
            sys.stdout.flush()
        if not self.in_thinking:
            self.in_thinking = True
        sys.stdout.write(f"\033[2m{text}\033[0m")
        sys.stdout.flush()

    def on_text(self, text: str) -> None:
        if not self.has_started:
            self.has_started = True
            self.spinner.cancel()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        if self.in_thinking:
            self.in_thinking = False
            sys.stdout.write("\n\033[2m──── 回答 ────\033[0m\n")
            sys.stdout.flush()
        sys.stdout.write(text)
        sys.stdout.flush()

    def on_tool_start(self, name: str, input: dict) -> None:
        self._stop_spinner()
        if self.in_thinking:
            self.in_thinking = False
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.console.print(f"  [yellow]●[/] {_tool_title(name, input)}")

    def on_tool_finish(
        self, name: str, input: dict, success: bool, preview: str, duration_ms: int
    ) -> None:
        icon = "[green]✓[/]" if success else "[red]✗[/]"
        dur = f" ({duration_ms / 1000:.1f}s)" if duration_ms else ""
        line = f"     {icon} {_tool_title(name, input)}{dur}"
        if not success:
            err = _short_preview(preview, 80)
            if err:
                line += f"  [dim]{err}[/]"
        self.console.print(line)


def _progress_hint(app: CodeForgeApp) -> str:
    """看门狗静默时说明「在等谁、等什么」，而不是笼统说"等模型响应"。

    父 Agent 执行工具（含子 Agent）期间自己的事件流是静默的 —— 生成器挂在
    `await` 上、不能 yield（结构性事实，实测确认过）。所以这里**主动去读**
    子 Agent 留下的进度记录（`core/tool/context.ProgressSink`）；读不到就退回原说法。

    实测踩过的误导：派了子 Agent 之后界面一直显示"仍在等待模型响应"，
    让人以为上游卡住，实际是子 Agent 在跑。
    """
    ctx = getattr(getattr(app, "agent", None), "_exec_ctx", None)
    sink = getattr(ctx, "progress", None)
    note = sink.snapshot() if sink is not None else None
    if note is None:
        return "模型响应"
    who = f"子 Agent「{note.agent}」" if note.agent else "子 Agent"
    what = note.action or "工作中"
    if note.detail:
        what = f"{what} {note.detail}"
    return f"{who}（{what}）"


def _render_compact_event(
    console: Console,
    event: CompactEvent,
) -> None:
    """渲染上下文压缩状态事件到 scrollback。"""
    if event.phase == CompactPhase.BEFORE_AUTO:
        console.print("[dim]正在压缩上下文...[/]")
    elif event.phase == CompactPhase.BEFORE_EMERGENCY:
        console.print("[dim]上下文撞墙，自动压缩中...[/]")
    elif event.err is not None:
        console.print(f"[red]压缩失败: {event.err}[/]")
    else:
        console.print(f"[dim]{format_compact_notice(event.before, event.after)}[/]")


_SPINNER_INTERVAL = 0.2


async def _spinner(stream, interval: float = 0.2) -> None:
    """后台加载动画，在首个 chunk 到达前持续刷新。"""
    frames = ["", ".", "..", "...", " ..", "  ..", "   ..."]
    i = 0
    try:
        while True:
            stream.write(f"\r\033[K\033[2m{''.join(frames[i % len(frames)])}\033[0m")
            stream.flush()
            i += 1
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
