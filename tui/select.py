"""终端共享选择组件 —— 方向键 + 回车（不依赖 prompt_toolkit）。

把 provider_select 的裸键读取抽出来，供 HITL 权限确认、Plan 审批、
恢复会话等所有「从选项里选一个」的场景复用。这些场景发生在 agent
事件循环中途，re-entrant 调用 prompt_toolkit 会导致终端状态错乱而卡死；
改用 msvcrt/termios 裸键读取可根治。
"""

from __future__ import annotations

import sys

from rich.console import Console

HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def _get_char() -> str:
    """读取单个字符（跨平台）。Windows 用 msvcrt，Unix 用 termios。"""
    if sys.platform == "win32":
        try:
            import msvcrt

            return msvcrt.getwch()
        except (ImportError, OSError, ValueError):
            pass

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        data = sys.stdin.buffer.read(1)
        while True:
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                data += sys.stdin.buffer.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_key() -> str:
    """读取一个导航键：UP / DOWN / ENTER / ESC / CTRL_C。

    非 TTY（管道 / CI）时直接返回 ENTER。Windows 用 msvcrt，Unix 用 termios。
    """
    if not sys.stdin.isatty():
        return "ENTER"

    if sys.platform == "win32":
        try:
            import msvcrt

            while True:
                ch = msvcrt.getwch()
                if ch == "\xe0":
                    ch2 = msvcrt.getwch()
                    mapping = {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}
                    return mapping.get(ch2, "ENTER")
                if ch in ("\r", "\n"):
                    return "ENTER"
                if ch == "\x03":
                    return "CTRL_C"
                if ch == "\x1b":
                    return "ESC"
                continue
        except (ImportError, OSError, ValueError):
            pass  # 回退到 Unix 路径

    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A":
                    return "UP"
                if seq == "[B":
                    return "DOWN"
                if seq == "[D":
                    return "LEFT"
                if seq == "[C":
                    return "RIGHT"
                return "ESC"
            if ch in ("\r", "\n"):
                return "ENTER"
            if ch == "\x03":
                return "CTRL_C"
            return "ENTER"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, OSError, AttributeError):
        # 没有 TTY（管道 / CI）—— 接受默认（第一项）
        return "ENTER"


def _ellipsize(text: str, width: int) -> str:
    """压成单行，超宽截断（按 CJK 双宽预留余量）。"""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _write_block(console: Console, lines: list[str]) -> None:
    """逐行写块；每行先清行，防止旧内容残留。"""
    for line in lines:
        console.print(f"\r\033[K{line}", soft_wrap=True)


def _block_height(visible_count: int, has_subtitle: bool) -> int:
    """blank(1) + title(1) [+ subtitle(1)] + sep(1) + options(n) + sep(1) + hint(1)。"""
    return 5 + visible_count + (1 if has_subtitle else 0)


def select_from_options(
    console: Console,
    *,
    title: str,
    options: list[str],
    subtitle: str = "",
    hint: str = "↑/↓ 选择，Enter 确认，Esc 取消",
    cancel_index: int = -1,
    window: int = 9,
) -> int:
    """方向键从 options 中选一个，返回选中下标。

    Esc / Ctrl+C 返回 cancel_index（默认 -1）。选项多时窗口滚动。
    渲染结束后会整体清掉对话框块。
    """
    n = len(options)
    if n == 0:
        return cancel_index

    width = max(console.width - 8, 20)
    shown = [_ellipsize(o, width) for o in options]
    has_subtitle = bool(subtitle.strip())
    if has_subtitle:
        subtitle = _ellipsize(subtitle, max(console.width - 4, 20))

    visible_count = min(window, n)
    height = _block_height(visible_count, has_subtitle)

    selected = 0
    first = 0

    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()
    try:
        while True:
            lines: list[str] = [""]
            lines.append(f"[bold]{title}[/]")
            if has_subtitle:
                lines.append(f"[dim]{subtitle}[/]")
            lines.append("-" * min(50, width + 2))
            for i in range(visible_count):
                idx = first + i
                label = shown[idx]
                if idx == selected:
                    lines.append(f"  [cyan]>[/] [bold]{label}[/]")
                else:
                    lines.append(f"    [dim]{label}[/]")
            lines.append("-" * min(50, width + 2))
            lines.append(f"[dim]{hint}[/]")
            _write_block(console, lines)

            key = get_key()

            if key == "UP":
                if selected > 0:
                    selected -= 1
            elif key == "DOWN":
                if selected < n - 1:
                    selected += 1
            elif key == "ENTER":
                break
            elif key in ("ESC", "CTRL_C"):
                return cancel_index
            else:
                continue

            if selected < first:
                first = selected
            elif selected >= first + visible_count:
                first = selected - visible_count + 1

            sys.stdout.write(f"\033[{height}A")
            sys.stdout.flush()
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
        # 清掉对话框块，让后续输出从原位继续
        sys.stdout.write(f"\r\033[{height}A\033[J")
        sys.stdout.flush()
    return selected


def read_line(console: Console, prompt: str) -> str:
    """裸单行文本输入；支持退格/回车；Esc/Ctrl+C 返回空串。"""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf: list[str] = []
    while True:
        ch = _get_char()
        if ch in ("\r", "\n"):
            break
        if ch in ("\x1b", "\x03"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return ""
        if ch in ("\b", "\x7f"):
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
        elif ch.isprintable() or ord(ch) > 127:
            buf.append(ch)
            sys.stdout.write(ch)
        sys.stdout.flush()
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(buf)
