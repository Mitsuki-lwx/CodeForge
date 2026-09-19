"""`tui.select.get_key()` 的按键解码。

这个函数此前**没有任何单测**，而它决定权限对话框能不能用方向键选——选错的后果不是
"难看"，是"按了没反应、再按回车就把权限批了"。

它的输入不是"一个字符"：Windows 上方向键是 CRT 翻出来的**两字节序列**，前缀是
`\\xe0` 还是 `\\x00` 取决于控制台记录里的 `ENHANCED_KEY` 标志，而这个标志**不由本程序
控制**——同一个物理按键两种编码，两种都必须认。

下面的序列不是猜的：用 `WriteConsoleInputW` 往真实控制台输入缓冲区注入按键事件
实测得到（`.workbuddy-ai/keypress_probe.py`，`--no-enhanced` 开关是那个对照实验）。
"""

from __future__ import annotations

import sys

import pytest

from tui import select


class _TtyStdin:
    def isatty(self) -> bool:
        return True


class _PipeStdin:
    def isatty(self) -> bool:
        return False


def _feed(monkeypatch, chars: str, *, tty: bool = True) -> None:
    """把 `msvcrt.getwch()` 换成按脚本吐字符的桩，并指定 stdin 是不是 TTY。"""
    import msvcrt

    it = iter(chars)
    monkeypatch.setattr(msvcrt, "getwch", lambda: next(it))
    monkeypatch.setattr(sys, "stdin", _TtyStdin() if tty else _PipeStdin())


def test_get_key_returns_enter_when_not_a_tty(monkeypatch):
    """管道 / CI 下直接回车，不阻塞（否则无头环境会挂死）。

    这条不限定平台：`isatty()` 检查在平台分支**之前**。
    字符脚本给空串——真去读就会 StopIteration，顺带证明它压根没读。
    """
    _feed(monkeypatch, "", tty=False)
    assert select.get_key() == "ENTER"


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt 分支只在 Windows 上成立")
@pytest.mark.parametrize(
    ("chars", "expected"),
    [
        ("\r", "ENTER"),
        ("\n", "ENTER"),
        ("\x03", "CTRL_C"),
        ("\x1b", "ESC"),
        # 扩展键，ENHANCED_KEY 置位 → \xe0 前缀（真实键盘实测就是这个）
        ("\xe0H", "UP"),
        ("\xe0P", "DOWN"),
        ("\xe0K", "LEFT"),
        ("\xe0M", "RIGHT"),
        # 同一个物理按键，控制台没标 ENHANCED_KEY → \x00 前缀，必须也认
        ("\x00H", "UP"),
        ("\x00P", "DOWN"),
        ("\x00K", "LEFT"),
        ("\x00M", "RIGHT"),
        # 前缀之后的字节不在映射里 → 落回 ENTER（既有行为，这里只锁住不变）
        ("\xe0X", "ENTER"),
        ("\x00X", "ENTER"),
        # 不认识的按键要被跳过、继续读下一个：不能把对话框卡死
        ("z\r", "ENTER"),
        ("zz\x1b", "ESC"),
    ],
)
def test_get_key_decodes_navigation_keys(monkeypatch, chars, expected):
    _feed(monkeypatch, chars)
    assert select.get_key() == expected
