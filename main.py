"""CodeForge — 多协议 LLM 终端对话客户端入口。"""

from __future__ import annotations


def main() -> None:
    """程序入口：加载配置 → 选 provider → 启动对话。"""
    from tui.app import run
    run()


if __name__ == "__main__":
    main()
