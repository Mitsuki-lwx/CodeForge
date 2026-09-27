"""落盘脱敏接入的验收（`docs/checklist_show_redact.md` §6）。

核心断言只有一句：**`audit/*.jsonl` 里 grep 不到凭据明文**。
其余是"不能因为加了脱敏就把既有行为改了"。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.observability.redact import reset_redaction_cache
from core.trace.events import HookEvent, PermissionEvent, ToolEndEvent
from core.trace.writer import REDACT_ERROR_KEY, TraceWriter

_A = "aB3"


def _secret(prefix: str = "ghp_", repeat: int = 10) -> str:
    return prefix + _A * repeat


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_redaction_cache()
    yield
    reset_redaction_cache()


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEFORGE_REDACT", "1")


def _p(root, sid: str):
    """TraceWriter 会在 audit_dir 下再建一层 `audit/`（writer.py:self._dir）。"""
    return root / "audit" / f"{sid}.jsonl"


def _lines(path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── §6 主断言：明文不落盘 ───────────────────────────────────────


def test_tool_end_preview_is_redacted_on_disk(tmp_path) -> None:
    w = TraceWriter("s1", audit_dir=tmp_path)
    w.record(ToolEndEvent(tool_name="Bash", result_preview="TOKEN=" + _secret()))
    w.close()

    raw = _p(tmp_path, "s1").read_text(encoding="utf-8")
    assert _secret() not in raw, "凭据明文落盘了"
    assert "[REDACTED:" in raw
    assert _lines(_p(tmp_path, "s1"))[0]["result_preview"].startswith("TOKEN=")


def test_permission_reason_is_redacted_on_disk(tmp_path) -> None:
    w = TraceWriter("s2", audit_dir=tmp_path)
    w.record(
        PermissionEvent(
            tool_name="Bash",
            decision="deny",
            reason="command contains sk-ant-api03-" + _A * 12,
        )
    )
    w.close()

    raw = _p(tmp_path, "s2").read_text(encoding="utf-8")
    assert "sk-ant-api03-" + _A * 12 not in raw
    assert "[REDACTED:anthropic_key]" in raw


def test_hook_reason_is_redacted_on_disk(tmp_path) -> None:
    w = TraceWriter("s3", audit_dir=tmp_path)
    w.record(HookEvent(tool_name="Bash", blocked=True, reason="leak " + _secret()))
    w.close()
    raw = _p(tmp_path, "s3").read_text(encoding="utf-8")
    assert _secret() not in raw


def test_dict_write_path_also_redacted(tmp_path) -> None:
    """`write(dict)` 是第二条入口，容易漏。"""
    w = TraceWriter("s4", audit_dir=tmp_path)
    w.write({"event": "x", "result_preview": _secret()})
    w.close()
    assert _secret() not in _p(tmp_path, "s4").read_text(encoding="utf-8")


# ── §6 不能改既有行为 ─────────────────────────────────────────


def test_disabled_redaction_keeps_plain_text(tmp_path, monkeypatch) -> None:
    """开关关掉 ⇒ 行为与今天**逐字一致**（明文落盘）。"""
    reset_redaction_cache()
    monkeypatch.setenv("CODEFORGE_REDACT", "0")
    w = TraceWriter("s5", audit_dir=tmp_path)
    w.record(ToolEndEvent(tool_name="Bash", result_preview="TOKEN=" + _secret()))
    w.close()
    assert _secret() in _p(tmp_path, "s5").read_text(encoding="utf-8")


def test_normal_content_untouched(tmp_path) -> None:
    """脱敏不许改动普通事件（键序/内容逐字不变）。"""
    w = TraceWriter("s6", audit_dir=tmp_path)
    w.record(ToolEndEvent(tool_name="Grep", success=True, result_preview="3 matches"))
    w.close()
    row = _lines(_p(tmp_path, "s6"))[0]
    assert row["result_preview"] == "3 matches"
    assert row["tool_name"] == "Grep"
    assert row["success"] is True


# ── §6 脱敏失败必须可发现、且不写明文 ─────────────────────────


def test_redaction_failure_drops_content_and_marks_error(
    tmp_path, monkeypatch
) -> None:
    """脱敏抛异常 ⇒ 不写明文，且留下 `redact_error` 痕迹。"""
    import core.trace.writer as wmod

    def boom(_data):
        raise RuntimeError("engine down")

    monkeypatch.setattr(wmod, "redact_mapping", boom)

    w = TraceWriter("s7", audit_dir=tmp_path)
    w.record(ToolEndEvent(tool_name="Bash", result_preview="TOKEN=" + _secret()))
    w.close()

    row = _lines(_p(tmp_path, "s7"))[0]
    assert REDACT_ERROR_KEY in row
    assert "RuntimeError" in row[REDACT_ERROR_KEY]
    assert "result_preview" not in row, "内容必须被丢掉，不能降级成明文"
    assert _secret() not in _p(tmp_path, "s7").read_text(encoding="utf-8")


def test_redaction_failure_keeps_structural_fields(tmp_path, monkeypatch) -> None:
    """降级行仍保留可用的结构信息（不只剩一个 error）。"""
    import core.trace.writer as wmod

    monkeypatch.setattr(wmod, "redact_mapping", lambda _d: (_ for _ in ()).throw(RuntimeError("x")))

    w = TraceWriter("s8", audit_dir=tmp_path)
    w.record(ToolEndEvent(tool_name="Bash", success=False, duration_ms=42,
                          result_preview=_secret()))
    w.close()

    row = _lines(_p(tmp_path, "s8"))[0]
    assert row["event"] == "tool_end"
    assert row["duration_ms"] == 42
    assert row["success"] is False
