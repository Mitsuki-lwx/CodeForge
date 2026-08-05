"""斜杠命令补全（prompt_toolkit Completer）。

候选来自命令注册中心：仅按命令名前缀匹配，隐藏命令不参与。
"""

from __future__ import annotations

from prompt_toolkit.completion import Completer, Completion


class CommandCompleter(Completer):
    """基于命令注册中心的补全器。"""

    def __init__(self, reg) -> None:
        self._reg = reg

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if "\n" in text:
            return  # 多行不激活补全
        if " " in text:
            return  # 已输入参数，只补命令名
        for cmd in self._reg.prefix_match(text):
            yield Completion(
                cmd.name,
                start_position=-len(text.lstrip("/")),
                display_meta=cmd.description,
            )
