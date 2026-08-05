"""终端 provider 选择（方向键交互，不依赖 prompt_toolkit）。"""

from __future__ import annotations

import sys
from typing import List

from rich.console import Console

from config.model import ProviderConfig
from tui.select import HIDE_CURSOR, SHOW_CURSOR, get_key


def select_provider(providers: List[ProviderConfig]) -> ProviderConfig:
    """方向键选择 provider。"""
    console = Console()
    n = len(providers)
    selected = 0

    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    try:
        while True:
            _render(console, providers, selected, n)
            key = get_key()

            if key == "UP" and selected > 0:
                selected -= 1
            elif key == "DOWN" and selected < n - 1:
                selected += 1
            elif key == "ENTER":
                break
            elif key in ("CTRL_C", "ESC"):
                sys.stdout.write(SHOW_CURSOR)
                sys.stdout.flush()
                console.print("\n[yellow]Canceled.[/]")
                raise SystemExit(0)

            # Move cursor up to overwrite: blank(1) + title(1) + sep(1) + n + sep(1) + hint(1) = 5 + n lines
            sys.stdout.write(f"\033[{5 + n}A")
            sys.stdout.flush()

    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()

    # Move back to top of selection block and clear everything below
    sys.stdout.write(f"\033[{5 + n}A")
    sys.stdout.write("\033[J")
    sys.stdout.flush()
    selected_provider = providers[selected]
    console.print(f"\n  [green]=>[/] {selected_provider.name} ({selected_provider.model})")
    return selected_provider


def _render(console: Console, providers: list, selected: int, n: int) -> None:
    """Render the selection UI block."""
    console.print()
    console.print("[bold]Select LLM Provider (up/down navigate, Enter confirm)[/]")
    console.print("-" * 50)

    for i, p in enumerate(providers):
        via = p.base_url or {
            "anthropic": "api.anthropic.com",
            "openai": "api.openai.com",
        }.get(p.protocol, p.protocol)
        thinking_flag = " [thinking]" if p.thinking else ""
        arrow = "[cyan]>[/]" if i == selected else " "
        style = "bold cyan" if i == selected else "dim"
        console.print(f"  {arrow} [{style}]{p.name}[/]  [dim]({p.model}, via {via}{thinking_flag})[/]")

    console.print("-" * 50)
    console.print("[dim]Press up/down to navigate, Enter to confirm[/]")
