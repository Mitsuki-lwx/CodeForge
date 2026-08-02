"""会话存档：JSONL 写入器 + ConversationManager 回调 单测。"""

from __future__ import annotations

import json
import time

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from conversation.message import Message, MessageRole, MessageStatus
from core.archive import (
    cleanup_expired,
    list_sessions,
    restore_session,
)
from core.archive.writer import CONVERSATION_FILENAME, Writer, serialize_message


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text, status=MessageStatus.COMPLETED)


# ── 序列化 ─────────────────────────────────────────────────────────


def test_serialize_message_fields():
    msg = _user("hello")
    data = serialize_message(msg, ts=1234, model="claude-test")
    assert data["role"] == "user"
    assert data["content"] == "hello"
    assert data["ts"] == 1234
    assert data["model"] == "claude-test"
    assert data["id"] == msg.id
    assert data["status"] == "completed"


def test_serialize_tool_message():
    msg = Message(
        role=MessageRole.ASSISTANT,
        content="",
        status=MessageStatus.COMPLETED,
        tool_use_id="tu-1",
        tool_name="bash",
        tool_input={"cmd": "ls"},
    )
    data = serialize_message(msg, model=None)
    assert data["tool_use_id"] == "tu-1"
    assert data["tool_name"] == "bash"
    assert data["tool_input"] == {"cmd": "ls"}
    assert "model" not in data  # 非首条不带 model


# ── Writer ─────────────────────────────────────────────────────────


def test_writer_appends_valid_json(tmp_path):
    with Writer(tmp_path, model="mock-model") as w:
        w.append(_user("u1"))
        w.append(_user("u2"))

    lines = (tmp_path / CONVERSATION_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["model"] == "mock-model"  # 首行带 model
    assert "model" not in second
    assert first["content"] == "u1"


def test_writer_compact_marker(tmp_path):
    with Writer(tmp_path) as w:
        w.append(_user("u1"))
        w.append_compact_marker()
        w.append(_user("u2"))
    lines = (tmp_path / CONVERSATION_FILENAME).read_text(encoding="utf-8").splitlines()
    marker = json.loads(lines[1])
    assert marker == {"type": "compact", "ts": marker["ts"]}
    assert marker["ts"] > 0


def test_writer_append_does_not_rewrite_existing(tmp_path):
    p = tmp_path / CONVERSATION_FILENAME
    p.write_text('{"role":"user","content":"old","ts":1}\n', encoding="utf-8")
    with Writer(tmp_path) as w:
        w.append(_user("new"))
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == "old"
    assert json.loads(lines[1])["content"] == "new"
    # 已有内容时首行不重复带 model
    assert "model" not in json.loads(lines[1])


# ── ConversationManager 回调 ───────────────────────────────────────


def test_callbacks_fire_on_append_points():
    appended = []
    conv = ConversationManager(on_append=appended.append)
    conv.add_user_message("hi")
    conv.add_assistant_message("hello")
    conv.add_tool_use("tu-1", "bash", {"cmd": "ls"})
    conv.add_tool_result("tu-1", "output")
    assert len(appended) == 4
    assert [m.role.value for m in appended] == [
        "user",
        "assistant",
        "assistant",
        "user",
    ]


def test_callback_fires_on_stream_finish():
    appended = []
    conv = ConversationManager(on_append=appended.append)
    m = conv.start_assistant_stream()  # 流式消息不立即触发
    assert len(appended) == 0
    conv.finish_stream(m, {"input_tokens": 1})
    assert len(appended) == 1
    assert appended[0].status == MessageStatus.COMPLETED


def test_callback_fires_on_replace():
    replaced = []
    conv = ConversationManager(on_replace=replaced.append)
    conv.replace_history([_user("summary"), _user("recent")])
    assert len(replaced) == 1
    assert len(replaced[0]) == 2


def test_no_callbacks_unchanged():
    conv = ConversationManager()
    conv.add_user_message("hi")
    conv.add_assistant_message("yo")
    conv.replace_history([_user("x")])
    assert [m.content for m in conv.messages] == ["x"]


def test_callback_exception_does_not_break(tmp_path):
    def boom(msg):
        raise RuntimeError("writer broken")

    conv = ConversationManager(on_append=boom)
    # 回调抛异常不影响对话继续
    conv.add_user_message("hi")
    assert conv.messages[0].content == "hi"


def test_full_writer_via_callbacks(tmp_path):
    writer = Writer(tmp_path, model="m")
    conv = ConversationManager(on_append=writer.append)
    conv.add_user_message("question")
    conv.add_assistant_message("answer")
    writer.close()

    lines = (tmp_path / CONVERSATION_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["role"] == "user"
    assert json.loads(lines[1])["role"] == "assistant"


# ── 会话列表扫描（F18-F20）────────────────────────────────────────


def _session_dir(tmp_path, sid, model="mock-model", body=None):
    """构造一个会话目录并写入 conversation.jsonl。"""
    d = tmp_path / ".codeforge" / "sessions" / sid
    d.mkdir(parents=True)
    lines = body or [
        '{"role":"user","content":"帮我看看这个项目","ts":1,"model":"mock-model"}',
        '{"role":"assistant","content":"好的","ts":2}',
    ]
    (d / "conversation.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def test_list_sessions_basic(tmp_path):
    _session_dir(tmp_path, "20260601-143022-a1b2")
    items = list_sessions(tmp_path)
    assert len(items) == 1
    item = items[0]
    assert item.session_id == "20260601-143022-a1b2"
    assert item.title == "帮我看看这个项目"
    assert item.model == "mock-model"
    assert item.size > 0


def test_list_sessions_title_truncated(tmp_path):
    long_title = "x" * 100
    body = [
        json.dumps({"role": "user", "content": long_title, "ts": 1, "model": "m"}),
        '{"role":"assistant","content":"ok","ts":2}',
    ]
    _session_dir(tmp_path, "20260601-143022-a1b2", body=body)
    items = list_sessions(tmp_path)
    assert len(items[0].title) == 50


def test_list_sessions_skips_old_format(tmp_path):
    _session_dir(tmp_path, "1717000000-abc12345")  # 旧格式
    _session_dir(tmp_path, "20260601-143022-a1b2")
    items = list_sessions(tmp_path)
    assert [i.session_id for i in items] == ["20260601-143022-a1b2"]


def test_list_sessions_sort_by_mtime_desc(tmp_path):
    d1 = _session_dir(tmp_path, "20260601-100000-0000")
    d2 = _session_dir(tmp_path, "20260602-100000-0000")
    # 显式设置不同 mtime 保证排序确定
    import os

    base = time.time()
    os.utime(d1 / "conversation.jsonl", (base, base))
    os.utime(d2 / "conversation.jsonl", (base, base + 10))
    items = list_sessions(tmp_path)
    assert items[0].session_id == "20260602-100000-0000"


def test_list_sessions_empty(tmp_path):
    assert list_sessions(tmp_path) == []


# ── 会话清理（F25/F26）────────────────────────────────────────────


def _old_session_dir(tmp_path, days_ago):
    from datetime import datetime, timedelta

    ts = datetime.now() - timedelta(days=days_ago)  # noqa: DTZ005 —— 仅生成会话 ID 字符串
    sid = ts.strftime("%Y%m%d-%H%M%S") + "-abcd"
    d = tmp_path / ".codeforge" / "sessions" / sid
    d.mkdir(parents=True)
    (d / "conversation.jsonl").write_text("x\n", encoding="utf-8")
    return d


def test_cleanup_removes_expired(tmp_path):
    old = _old_session_dir(tmp_path, 31)
    cleanup_expired(tmp_path, days=30)
    assert not old.exists()


def test_cleanup_keeps_fresh(tmp_path):
    fresh = _old_session_dir(tmp_path, 5)
    cleanup_expired(tmp_path, days=30)
    assert fresh.exists()


def test_cleanup_keeps_old_format(tmp_path):
    old_fmt = tmp_path / ".codeforge" / "sessions" / "1717000000-abc12345"
    old_fmt.mkdir(parents=True)
    cleanup_expired(tmp_path, days=30)
    assert old_fmt.exists()


# ── 恢复（F21）────────────────────────────────────────────────────

_MOCK_PROVIDER = ProviderConfig(
    name="mock", protocol="anthropic", model="mock", api_key="mock"
)


def _jsonl_session(tmp_path, sid, lines):
    d = tmp_path / ".codeforge" / "sessions" / sid
    d.mkdir(parents=True)
    (d / "conversation.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


async def test_restore_basic(tmp_path):
    now = int(time.time())
    d = _jsonl_session(
        tmp_path,
        "20260601-143022-a1b2",
        [
            json.dumps({"role": "user", "content": "hi", "ts": now}),
            json.dumps({"role": "assistant", "content": "hello", "ts": now + 1}),
        ],
    )
    result = await restore_session(
        d, provider_config=_MOCK_PROVIDER, context_window=200000
    )
    assert [m.content for m in result.conversation.messages] == ["hi", "hello"]
    assert result.skipped == 0


async def test_restore_skips_bad_line(tmp_path):
    now = int(time.time())
    d = _jsonl_session(
        tmp_path,
        "20260601-143022-a1b2",
        [
            json.dumps({"role": "user", "content": "u1", "ts": now}),
            "this is not json",
            json.dumps({"role": "assistant", "content": "a1", "ts": now + 1}),
        ],
    )
    result = await restore_session(
        d, provider_config=_MOCK_PROVIDER, context_window=200000
    )
    assert [m.content for m in result.conversation.messages] == ["u1", "a1"]
    assert result.skipped == 1


async def test_restore_truncates_dangling_tool_use(tmp_path):
    now = int(time.time())
    d = _jsonl_session(
        tmp_path,
        "20260601-143022-a1b2",
        [
            json.dumps({"role": "user", "content": "u1", "ts": now}),
            json.dumps({"role": "assistant", "content": "a1", "ts": now + 1}),
            json.dumps(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_use_id": "tu1",
                    "tool_name": "bash",
                    "ts": now + 2,
                }
            ),
        ],
    )
    result = await restore_session(
        d, provider_config=_MOCK_PROVIDER, context_window=200000
    )
    assert [m.content for m in result.conversation.messages] == ["u1", "a1"]


async def test_restore_from_last_compact_marker(tmp_path):
    now = int(time.time())
    d = _jsonl_session(
        tmp_path,
        "20260601-143022-a1b2",
        [
            json.dumps({"role": "user", "content": "OLD", "ts": now}),
            json.dumps({"type": "compact", "ts": now + 1}),
            json.dumps({"role": "user", "content": "NEW", "ts": now + 2}),
        ],
    )
    result = await restore_session(
        d, provider_config=_MOCK_PROVIDER, context_window=200000
    )
    assert [m.content for m in result.conversation.messages] == ["NEW"]


async def test_restore_time_reminder(tmp_path):
    stale_ts = int(time.time()) - 7 * 3600  # 7 小时前
    d = _jsonl_session(
        tmp_path,
        "20260601-143022-a1b2",
        [json.dumps({"role": "user", "content": "hi", "ts": stale_ts})],
    )
    result = await restore_session(
        d, provider_config=_MOCK_PROVIDER, context_window=200000
    )
    last = result.conversation.messages[-1]
    assert "本会话已暂停" in last.content
    assert result.time_gap_seconds > 6 * 3600


async def test_restore_no_reminder_when_fresh(tmp_path):
    now_ts = int(time.time())
    d = _jsonl_session(
        tmp_path,
        "20260601-143022-a1b2",
        [json.dumps({"role": "user", "content": "hi", "ts": now_ts})],
    )
    result = await restore_session(
        d, provider_config=_MOCK_PROVIDER, context_window=200000
    )
    assert len(result.conversation.messages) == 1
    assert "本会话已暂停" not in result.conversation.messages[-1].content


async def test_restore_compact_on_token_overrun(tmp_path, monkeypatch):
    from llm.client import LLMClient
    from llm.stream_events import CompletionDone, TextChunk

    class FakeSummary(LLMClient):
        async def stream_chat(
            self, messages, system_prompt="", tools=None, system_blocks=None
        ):
            yield TextChunk(text="<summary>RESTORE_SUMMARY</summary>")
            yield CompletionDone()

    monkeypatch.setattr(LLMClient, "create", lambda cfg: FakeSummary(_MOCK_PROVIDER))
    # 估算超过 window=40000 的阈值 7000 token → 触发压缩
    big = "x" * 30000
    d = _jsonl_session(
        tmp_path,
        "20260601-143022-a1b2",
        [json.dumps({"role": "user", "content": big, "ts": 1})],
    )
    result = await restore_session(
        d, provider_config=_MOCK_PROVIDER, context_window=40000
    )
    assert result.compacted is True
    assert "RESTORE_SUMMARY" in result.conversation.messages[0].content
