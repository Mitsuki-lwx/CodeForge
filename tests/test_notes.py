"""笔记存储层单测。"""

from __future__ import annotations

import json
import re

import pytest

from core.notes import build_memory_index_text, update_memory
from core.notes.store import NoteStore


@pytest.fixture
def store(tmp_path) -> NoteStore:
    home = tmp_path / "home"
    return NoteStore(workspace=tmp_path / "proj", user_home=home)


# ── 创建与索引 ────────────────────────────────────────────────────


def test_create_user_preference_note(store, tmp_path):
    path = store.create_note(
        "user", "user_preference", "简洁回复", "terse_replies", "用户偏好简洁回复。"
    )
    assert path.name == "user_preference_terse_replies.md"
    assert str(path.parent) == str(tmp_path / "home" / ".codeforge" / "memory")
    text = path.read_text(encoding="utf-8")
    assert "type: user_preference" in text
    assert "title: 简洁回复" in text
    assert "created:" in text


def test_create_project_knowledge_note(store, tmp_path):
    path = store.create_note(
        "project", "project_knowledge", "API 约定", "api_conventions", "用 GET 拉数据。"
    )
    assert str(path.parent) == str(tmp_path / "proj" / ".codeforge" / "memory")
    assert path.name == "project_knowledge_api_conventions.md"


def test_index_line_appears(store):
    store.create_note(
        "project", "project_knowledge", "API 约定", "api", "用 GET 拉数据。"
    )
    store.create_note("user", "user_preference", "简洁回复", "terse", "偏好简洁。")
    project_index = store.list_index("project")
    user_index = store.list_index("user")
    assert any(
        "- [project_knowledge] API 约定 — 用 GET 拉数据。" in l for l in project_index
    )
    assert any("- [user_preference] 简洁回复 — 偏好简洁。" in l for l in user_index)


def test_full_index_project_first(store):
    store.create_note("project", "project_knowledge", "P", "p", "ppp")
    store.create_note("user", "user_preference", "U", "u", "uuu")
    text = store.full_index()
    assert text.index("project_knowledge") < text.index("user_preference")


def test_create_rejects_unknown_type(store):
    with pytest.raises(ValueError):
        store.create_note("project", "evil_type", "t", "s", "c")


def test_slug_sanitized(store):
    path = store.create_note("project", "project_knowledge", "t", "../evil", "c")
    assert path.name == "project_knowledge_evil.md"
    assert path.parent == store._project_dir


# ── 更新与删除 ────────────────────────────────────────────────────


def test_update_preserves_created(store):
    store.create_note("user", "user_preference", "简洁回复", "terse", "v1")
    p = store._user_dir / "user_preference_terse.md"
    created_before = p.read_text(encoding="utf-8")
    store.update_note("user", "user_preference_terse.md", "更简洁", "v2 内容")
    text = p.read_text(encoding="utf-8")
    assert "title: 更简洁" in text
    assert "v2 内容" in text
    # created 保持不变，updated 更新
    m_created = re.search(r"created: (\S+)", created_before).group(1)
    assert f"created: {m_created}" in text
    # 索引更新为新标题
    assert any("更简洁" in l for l in store.list_index("user"))


def test_delete_removes_file_and_index(store):
    store.create_note("user", "user_preference", "t", "x", "c")
    store.delete_note("user", "user_preference_x.md")
    assert not (store._user_dir / "user_preference_x.md").exists()
    assert store.list_index("user") == []


def test_filename_traversal_blocked(store):
    store.create_note("user", "user_preference", "t", "x", "c")
    with pytest.raises(ValueError):
        store.update_note("user", "../../escape.md", "t", "c")


# ── 读操作 ────────────────────────────────────────────────────────


def test_read_note(store):
    store.create_note("user", "user_preference", "t", "x", "body text")
    text = store.read_note("user", "user_preference_x.md")
    assert "body text" in text
    assert "type: user_preference" in text


def test_index_rebuild_after_second_create(store):
    store.create_note("user", "user_preference", "a", "a", "aaa")
    store.create_note("user", "correction_feedback", "b", "b", "bbb")
    assert len(store.list_index("user")) == 2


# ── 触发判断（F35）────────────────────────────────────────────────


def test_should_trigger_every_5_turns():
    from core.notes import should_trigger_memory

    assert should_trigger_memory(5, "随便聊聊")
    assert should_trigger_memory(10, "随便聊聊")
    assert not should_trigger_memory(3, "随便聊聊")


def test_should_trigger_by_keyword():
    from core.notes import should_trigger_memory

    assert should_trigger_memory(1, "请记住这个偏好")
    assert should_trigger_memory(2, "remember this")
    assert should_trigger_memory(2, "MEMO 一下")
    assert not should_trigger_memory(2, "随便聊聊")


# ── JSON 提取 ─────────────────────────────────────────────────────


def test_extract_json_array_valid():
    from core.notes.updater import _extract_json_array

    assert _extract_json_array('[{"action":"create"}]') == [{"action": "create"}]


def test_extract_json_array_wrapped():
    from core.notes.updater import _extract_json_array

    assert _extract_json_array('好的，结果如下：\n[{"action":"delete"}]') == [
        {"action": "delete"}
    ]


def test_extract_json_array_garbage():
    from core.notes.updater import _extract_json_array

    assert _extract_json_array("完全没有 JSON") == []


# ── 记忆更新执行（F39/F40）────────────────────────────────────────

from config.model import ProviderConfig
from conversation.message import Message, MessageRole, MessageStatus
from llm.client import LLMClient
from llm.stream_events import CompletionDone, TextChunk

_NOTE_PROVIDER = ProviderConfig(
    name="mock", protocol="anthropic", model="mock", api_key="mock"
)


class _FakeMemoryClient(LLMClient):
    def __init__(self, text):
        super().__init__(_NOTE_PROVIDER)
        self.text = text
        self.tools = []

    async def stream_chat(
        self, messages, system_prompt="", tools=None, system_blocks=None
    ):
        self.tools.append(tools)
        yield TextChunk(text=self.text)
        yield CompletionDone()


def _recent(text="hello"):
    return [
        Message(role=MessageRole.USER, content=text, status=MessageStatus.COMPLETED),
        Message(
            role=MessageRole.ASSISTANT, content="ok", status=MessageStatus.COMPLETED
        ),
    ]


async def test_update_memory_create(store, monkeypatch):
    ops = json.dumps(
        [
            {
                "action": "create",
                "level": "project",
                "type": "project_knowledge",
                "title": "API 约定",
                "slug": "api_conventions",
                "content": "用 GET 拉数据",
            }
        ]
    )
    client = _FakeMemoryClient(ops)
    monkeypatch.setattr(LLMClient, "create", lambda cfg: client)
    await update_memory(_NOTE_PROVIDER, store, _recent())
    assert client.tools == [None]  # 更新请求不传工具
    assert (store._project_dir / "project_knowledge_api_conventions.md").exists()
    assert any("API 约定" in l for l in store.list_index("project"))


async def test_update_memory_update_and_delete(store, monkeypatch):
    store.create_note("user", "user_preference", "简洁回复", "terse", "v1")
    store.create_note("user", "correction_feedback", "旧反馈", "old", "old body")
    ops = json.dumps(
        [
            {
                "action": "update",
                "level": "user",
                "filename": "user_preference_terse.md",
                "title": "更简洁",
                "content": "v2 body",
            },
            {
                "action": "delete",
                "level": "user",
                "filename": "correction_feedback_old.md",
            },
        ]
    )
    client = _FakeMemoryClient(ops)
    monkeypatch.setattr(LLMClient, "create", lambda cfg: client)
    await update_memory(_NOTE_PROVIDER, store, _recent())
    text = store.read_note("user", "user_preference_terse.md")
    assert "v2 body" in text
    assert "title: 更简洁" in text
    assert not (store._user_dir / "correction_feedback_old.md").exists()


async def test_update_memory_no_ops(store, monkeypatch):
    client = _FakeMemoryClient("[]")
    monkeypatch.setattr(LLMClient, "create", lambda cfg: client)
    await update_memory(_NOTE_PROVIDER, store, _recent())
    assert not store.list_index("project")
    assert not store.list_index("user")


async def test_update_memory_failure_silent(store, monkeypatch):
    class _Boom(LLMClient):
        async def stream_chat(
            self, messages, system_prompt="", tools=None, system_blocks=None
        ):
            raise RuntimeError("provider down")

    monkeypatch.setattr(LLMClient, "create", lambda cfg: _Boom(_NOTE_PROVIDER))
    # 失败不抛异常、不写文件
    await update_memory(_NOTE_PROVIDER, store, _recent())
    assert not store.list_index("project")


# ── 记忆索引注入（F32-F34）────────────────────────────────────────


async def test_build_memory_index_truncation(store, monkeypatch):
    store.create_note("project", "project_knowledge", "x" * 200, "t", "y" * 200)
    monkeypatch.setattr("core.notes.inject.INDEX_MAX_BYTES", 100)
    text = build_memory_index_text(store)
    assert text.endswith("(index truncated)")
    assert len(text.encode("utf-8")) <= 100 + len("(index truncated)")


async def test_load_and_inject_memory(store):
    from core.notes import load_and_inject_memory
    from core.prompts.builder import PromptBuilder

    store.create_note("project", "project_knowledge", "API 约定", "api", "用 GET")
    builder = PromptBuilder()
    load_and_inject_memory(builder, store)
    assembly = builder.build_assembly()
    stable = "\n".join(b.content for b in assembly.cached)
    assert "project_knowledge" in stable
