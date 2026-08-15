"""CodeForge — 多协议 LLM 终端对话客户端入口。"""

from __future__ import annotations

import argparse
import sys


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CLI 参数；`--team-member` 出现时走 pane 队友自治循环分支。"""
    p = argparse.ArgumentParser(prog="codeforge", description="终端 AI 编程助手")
    p.add_argument(
        "--team-member",
        action="store_true",
        help="以 pane 队友自治循环启动（由 Team spawn 调用）",
    )
    p.add_argument("--team", default="")
    p.add_argument("--member", default="")
    p.add_argument("--agent-id", default="")
    p.add_argument("--session-dir", default="")
    p.add_argument("--worktree", default="")
    p.add_argument("--agent-type", default="")
    p.add_argument("--model", default="")
    p.add_argument("--plan-mode", action="store_true")
    p.add_argument(
        "--task", default="", help="Run a single task headlessly (skip TUI), then exit"
    )
    p.add_argument(
        "--loop", default="", help="Agent 循环策略：react 或自定义模块路径（spec_loop）"
    )
    return p.parse_args(argv)


def main() -> None:
    """程序入口：加载配置 → 选 provider → 启动对话。"""
    # Windows: 强制 UTF-8 编码以支持中文 IME 输入
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001, S110 —— 部分流可能不支持 reconfigure，忽略
            pass

    # 可观测性：Logs / Metrics / Traces 初始化(未启用/无依赖时安全 no-op)
    try:
        from core.observability import ensure_initialized

        ensure_initialized()
    except Exception:  # noqa: BLE001 —— 可观测性失败绝不阻塞启动
        pass

    args = _parse_args()
    if args.team_member:
        # pane 队友子进程：不启动 TUI，跑自治循环
        from core.team.team_member import run_team_member_entry

        run_team_member_entry(args)
        return

    from tui.app import run

    run(task=args.task, loop=args.loop)


if __name__ == "__main__":
    main()
