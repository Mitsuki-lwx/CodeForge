"""Markdown 渲染工具。

将纯文本转换为 Rich 富文本格式，用于对话区展示。"""

from __future__ import annotations

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text as RichText


def render_markdown(text: str) -> RichMarkdown:
    """将文本以 Markdown 方式渲染为 Rich 可渲染对象。"""
    return RichMarkdown(text)


def render_plain(text: str) -> RichText:
    """将文本以纯文本方式渲染。"""
    return RichText(text)
