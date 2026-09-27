"""补齐脱敏缺口（P0/P1/P2）的测试。

对应 `docs/spec_redact_gaps.md`。
P0 = 落盘截断窗口、P1 = 关闭后写入可见化、P2 = split_rounds 角色判据。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from core.agent.config import AgentConfig
from core.task.view import split_rounds
from core.trace.events import ToolEndEvent
from core.trace.writer import TraceWriter

# 假凭据：形态与真 anthropic key 相同（vendor 前缀 + 混合大小写数字）
FAKE_KEY = "sk-ant-api03-" + "aB3xQ9mZ7pL2kR8tY4wN6vC1dF5gH0jK3"


def _p(root: str, sid: str = "s1") -> Path:
    """audit 真实路径（writer 内部还会再拼一层 `audit/`）。"""
    return Path(root) / "audit" / f"{sid}.jsonl"


def _lines(root: str, sid: str = "s1") -> list[str]:
    f = _p(root, sid)
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


# ── P0：落盘截断窗口 ──────────────────────────────────────────────


class TestPreviewWindow:
    """截断在脱敏**之前**发生，所以窗口宽度直接决定脱敏覆盖率。"""

    def test_window_is_wide_enough_to_be_useful(self) -> None:
        """★ 默认窗口必须显著大于最初的 120。

        120 的问题不是"短"，是**短到让脱敏够不着**：
        窗口外的凭据根本不落盘 ⇒ 既无明文也无痕迹可查。
        """
        assert AgentConfig().result_preview_max_chars >= 1000

    def test_credential_beyond_old_window_leaves_no_trace(self) -> None:
        """旧窗口(120)下，埋在深处的凭据在 audit 里**零痕迹**。

        这条锁的是"缺口曾经存在"——若将来窗口再被调小，本测试提醒你
        脱敏覆盖率会跟着退回去。
        """
        content = f"env dump:\n{'x' * 1000}\nANTHROPIC_API_KEY={FAKE_KEY}\n"
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            w.record(
                ToolEndEvent(
                    tool_use_id="t1",
                    tool_name="Bash",
                    success=True,
                    duration_ms=1,
                    result_preview=content[:120],
                )
            )
            w.close()
            blob = "\n".join(_lines(td))
        assert FAKE_KEY not in blob
        assert "ANTHROPIC" not in blob, "120 窗口下应完全无痕迹"

    def test_credential_inside_new_window_is_redacted(self) -> None:
        """★ 同样的内容放进 2000 窗口 ⇒ 被遮住且留下标记。"""
        content = f"env dump:\n{'x' * 1000}\nANTHROPIC_API_KEY={FAKE_KEY}\n"
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            w.record(
                ToolEndEvent(
                    tool_use_id="t1",
                    tool_name="Bash",
                    success=True,
                    duration_ms=1,
                    result_preview=content[: AgentConfig().result_preview_max_chars],
                )
            )
            w.close()
            blob = "\n".join(_lines(td))
        assert FAKE_KEY not in blob, "凭据明文泄漏"
        assert "[REDACTED:" in blob, "应留下遮蔽标记"

    def test_tui_display_width_is_independent_of_disk_window(self) -> None:
        """★ 显示与落盘是**两个独立常量**，放宽落盘不动界面。

        这是放宽 `result_preview_max_chars` 的前提。将来若有人把两者"统一"，
        本测试会挡住那次改动。
        """
        from tui.app import _short_preview

        long_out = "env dump:\n" + "x" * 5000
        # 显示只取首行，且长度上限 60 —— 与落盘窗口(2000)毫无关系
        assert _short_preview(long_out) == "env dump:"
        assert len(_short_preview(long_out)) < 100
        assert AgentConfig().result_preview_max_chars > 1000

    def test_widening_window_does_not_corrupt_structure(self) -> None:
        content = "x" * 1500 + f"\nKEY={FAKE_KEY}"
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            w.record(
                ToolEndEvent(
                    tool_use_id="t1",
                    tool_name="Bash",
                    success=True,
                    duration_ms=7,
                    result_preview=content,
                )
            )
            w.close()
            ev = json.loads(_lines(td)[0])
        assert ev["tool_name"] == "Bash"
        assert ev["success"] is True
        assert ev["duration_ms"] == 7
        assert ev["sequence"] >= 1
        assert ev["ts"] > 0


# ── P1：关闭后写入不再静默 ────────────────────────────────────────


class TestDroppedAfterClose:
    """`agent.run()` 的 finally 已 `_trace_close()`，收尾期晚到的写入是常态。

    静默丢弃会让"注进去的东西没生效"查无实据（本轮探针为此连续假红 6 轮）。
    """

    def test_record_after_close_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            w.close()
            w.record(ToolEndEvent(tool_use_id="t1", tool_name="Bash", success=True))
            w.write({"event": "x"})

    def test_dropped_count_increments(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            assert w.dropped_after_close == 0
            w.close()
            w.record(ToolEndEvent(tool_use_id="t1", tool_name="Bash", success=True))
            w.record(ToolEndEvent(tool_use_id="t2", tool_name="Bash", success=True))
            w.write({"event": "x"})
            assert w.dropped_after_close == 3

    def test_dropped_count_unchanged_while_open(self) -> None:
        """正常写入不该被计入丢弃。"""
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            w.record(ToolEndEvent(tool_use_id="t1", tool_name="Bash", success=True))
            w.write({"event": "x"})
            assert w.dropped_after_close == 0
            w.close()

    def test_nothing_is_written_after_close(self) -> None:
        """计数之外，仍要保证关闭后**真的**没写进文件。"""
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            w.record(ToolEndEvent(tool_use_id="t1", tool_name="Bash", success=True))
            w.close()
            before = len(_lines(td))
            w.record(ToolEndEvent(tool_use_id="t2", tool_name="Bash", success=True))
            assert len(_lines(td)) == before

    def test_drop_is_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with tempfile.TemporaryDirectory() as td, caplog.at_level(
            logging.DEBUG, logger="core.trace.writer"
        ):
            w = TraceWriter("s1", audit_dir=td)
            w.close()
            w.record(ToolEndEvent(tool_use_id="t1", tool_name="Bash", success=True))
        assert any("已关闭" in r.message or "丢弃" in r.message for r in caplog.records)

    def test_close_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            w = TraceWriter("s1", audit_dir=td)
            w.close()
            w.close()
            assert w.dropped_after_close == 0


# ── P2：split_rounds 角色判据 ─────────────────────────────────────


class _Msg:
    """裸 role 的最小消息（模拟绕过 Message 直接塞值的调用方）。"""

    def __init__(self, role: Any, content: str, tool_use_id: str | None = None) -> None:
        self.role = role
        self.content = content
        self.tool_use_id = tool_use_id


class TestRoundRoleRobustness:
    def test_plain_string_role_is_accepted(self) -> None:
        """★ 裸字符串 `role="user"` 也要能切出轮。

        旧判据 `getattr(role,"value","")` 对裸字符串静默返回假 ——
        1000 轮曾被切成 1 轮而不报错。
        """
        msgs = [_Msg("user", f"第{i}轮") for i in range(3)]
        assert len(split_rounds(msgs)) == 3

    def test_enum_role_still_works(self) -> None:
        from conversation.message import Message, MessageRole

        msgs = [
            Message(role=MessageRole.USER, content=f"第{i}轮") for i in range(3)
        ]
        assert len(split_rounds(msgs)) == 3

    def test_both_shapes_give_same_rounds(self) -> None:
        """两种传法切出的轮数必须一致 —— 这是改动的核心断言。"""
        from conversation.message import Message, MessageRole

        plain = [_Msg("user", f"第{i}轮") for i in range(5)]
        enum = [Message(role=MessageRole.USER, content=f"第{i}轮") for i in range(5)]
        a, b = split_rounds(plain), split_rounds(enum)
        assert len(a) == len(b) == 5

    def test_assistant_role_never_starts_round(self) -> None:
        assert len(split_rounds([_Msg("assistant", "回答")])) == 1
        assert len(split_rounds([_Msg("system", "系统")])) == 1

    def test_missing_role_is_not_a_round(self) -> None:
        class NoRole:
            content = "孤儿消息"

        assert len(split_rounds([NoRole()])) == 1

    def test_tool_result_still_does_not_start_round(self) -> None:
        """防回归：P2 改动不得破坏"工具结果不开轮"这条原有规则。"""
        msgs = [
            _Msg("user", "问题"),
            _Msg("user", "结果", tool_use_id="t1"),
            _Msg("assistant", "回答"),
            _Msg("user", "下一问"),
        ]
        assert len(split_rounds(msgs)) == 2

    def test_system_reminder_still_does_not_start_round(self) -> None:
        """防回归：`[system_reminder]` 不开轮。"""
        msgs = [
            _Msg("user", "问题"),
            _Msg("user", "[system_reminder]提醒"),
            _Msg("user", "下一问"),
        ]
        assert len(split_rounds(msgs)) == 2

    def test_large_session_with_plain_roles_keeps_all_rounds(self) -> None:
        """★ 复现原 bug 的规模：1000 轮裸字符串必须切出 1000 轮。"""
        msgs = [_Msg("user", f"第{i}轮的问题 " * 10) for i in range(1000)]
        rounds = split_rounds(msgs)
        assert len(rounds) == 1000
        assert rounds[0].number == 1
        assert rounds[-1].number == 1000
