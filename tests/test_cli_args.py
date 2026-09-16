"""CLI 参数解析（main._parse_args）—— 子命令分发的回归护栏。

这里钉住一个**设计假设**：子命令用可选位置参数分发，而不是 argparse 的 subparsers，
因为 Team spawn 起的 pane 队友进程传的是一串平铺 flag（`--team-member --team X
--member Y ...`）。改 subparsers 会破坏这些调用方，而"某个 flag 的值恰好等于
`host`/`runs`/`attach`"正是会把假设打穿的地方——所以必须有测试守着。
"""

from __future__ import annotations

import pytest

from main import _parse_args


def test_no_args_enters_tui() -> None:
    args = _parse_args([])
    assert args.command == ""
    assert args.team_member is False


def test_host_and_runs_subcommands() -> None:
    assert _parse_args(["host"]).command == "host"
    assert _parse_args(["runs"]).command == "runs"


def test_attach_takes_target_and_continue() -> None:
    args = _parse_args(["attach", "run-1", "--continue"])
    assert args.command == "attach"
    assert args.target == "run-1"
    assert args.resume is True
    assert args.prompt == ""


def test_attach_continue_accepts_prompt() -> None:
    args = _parse_args(["attach", "run-1", "--continue", "--prompt", "接着上次的说"])
    assert args.resume is True
    assert args.prompt == "接着上次的说"


def test_attach_without_target_is_not_an_error_here() -> None:
    # 缺 run_id 由 _cmd_attach 报可读错误（这里只保证解析不炸）
    args = _parse_args(["attach"])
    assert args.command == "attach"
    assert args.target == ""


@pytest.mark.parametrize("value", ["host", "runs", "attach"])
def test_option_value_is_never_mistaken_for_subcommand(value: str) -> None:
    """`--member host` 必须落到 --member，而不是被子命令位置参数抢走。

    这是 subparsers 方案会破坏、而位置参数方案必须成立的那条边界。
    """
    args = _parse_args(["--team-member", "--team", "t1", "--member", value])
    assert args.command == ""
    assert args.member == value
    assert args.team_member is True


def test_task_value_is_never_mistaken_for_subcommand() -> None:
    args = _parse_args(["--task", "runs"])
    assert args.command == ""
    assert args.task == "runs"


def test_existing_flags_still_parse(tmp_path) -> None:
    """既有调用方（Team spawn / 无头任务）的 flag 组合不能被破坏。"""
    args = _parse_args(
        [
            "--team-member",
            "--team",
            "team-a",
            "--member",
            "m1",
            "--agent-id",
            "a1",
            "--session-dir",
            str(tmp_path),
            "--worktree",
            "w1",
            "--agent-type",
            "coder",
            "--model",
            "m",
            "--plan-mode",
            "--loop",
            "react",
        ]
    )
    assert args.command == ""
    assert args.team == "team-a"
    assert args.member == "m1"
    assert args.agent_type == "coder"
    assert args.plan_mode is True
    assert args.loop == "react"
