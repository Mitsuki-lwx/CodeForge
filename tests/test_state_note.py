"""会话状态（spec_session_state）单测：存储 / 注入 / 不可压缩。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.notes.state import SessionStateStore
from core.notes.store import NoteStore


def _make():
    d = Path(tempfile.mkdtemp())
    notes = NoteStore(workspace=d / "proj", user_home=d / "home")
    state = SessionStateStore(d / "sessions" / "s1", notes=notes)
    return d, state, notes


# ── 目标 ─────────────────────────────────────────────────────────

def test_goal_set_get():
    _, s, _ = _make()
    assert s.get_goal() is None
    s.set_goal("修 bug")
    assert s.get_goal() == "修 bug"
    s.set_goal("新目标")  # 覆盖
    assert s.get_goal() == "新目标"


# ── 待办 ─────────────────────────────────────────────────────────

def test_todo_add_toggle():
    _, s, _ = _make()
    s.add_todo("先跑测试")
    todos = s.list_todos()
    assert len(todos) == 1 and todos[0]["done"] is False
    slug = todos[0]["id"].replace("task_todo_", "").replace(".md", "")
    s.toggle_todo(slug, True)
    assert s.list_todos()[0]["done"] is True


# ── 约束 ─────────────────────────────────────────────────────────

def test_constraint_session_default():
    _, s, _ = _make()
    s.add_constraint("别改 test.py")
    cons = s.list_constraints()
    assert len(cons) == 1 and cons[0]["level"] == "session"


def test_constraint_persist_and_promote():
    _, s, notes = _make()
    # persist=project 直接提升
    s.add_constraint("项目用 pytest", persist="project")
    assert any(
        i["body"] == "项目用 pytest"
        for i in notes.list_notes("project")
        if i["type"] == "hard_constraint"
    )
    # 会话级再 promote 到 user
    s.add_constraint("别改 config")
    slug = s.list_constraints()[0]["id"].replace("hard_constraint_", "").replace(".md", "")
    s.promote_constraint(slug, "user")
    assert any(
        i["body"] == "别改 config"
        for i in notes.list_notes("user")
        if i["type"] == "hard_constraint"
    )
    # 会话级副本已删除，避免重复注入
    assert s.list_constraints() == []


def test_constraint_never_default_persist():
    """约束默认会话级；持久必须显式（persist / promote）。"""
    _, s, notes = _make()
    s.add_constraint("默认会话级")
    assert notes.list_notes("project") == []
    assert notes.list_notes("user") == []


# ── 注入 ─────────────────────────────────────────────────────────

def test_state_injection_blocks():
    """约束进 cached，目标+待办进 uncached（spec 缓存确定性）。"""
    from core.prompts.builder import PromptBuilder

    b = PromptBuilder()
    b.set_state(
        constraints="## 硬性约束\n- 别改 x",
        goals_todos="## 当前目标\n修 bug\n## 待办\n- [ ] 先跑测试",
    )
    a = b.build_assembly(env_info="env: /tmp")
    assert "别改 x" in a.cached[-1].content
    assert "修 bug" in a.uncached[-1].content
    # 缓存稳定：相同状态两次一致
    a2 = b.build_assembly(env_info="env: /tmp")
    assert [c.content for c in a.cached] == [c.content for c in a2.cached]


def test_state_not_in_conv_messages():
    """不可压缩契约：状态注入在 PromptBuilder 层，不在 conv.messages（manage_context 不碰）。"""
    from conversation.manager import ConversationManager
    from core.prompts.builder import PromptBuilder

    b = PromptBuilder()
    b.set_state(constraints="## 硬性约束\n- 别改 x", goals_todos="## 当前目标\n修 bug")
    conv = ConversationManager()
    conv.add_user_message("hello")
    api_msgs, _ = conv.to_api_format()
    # 状态文本不出现在对话消息里（它只出现在 PromptBuilder 的 assembly）
    joined = "\n".join(getattr(m, "content", "") if isinstance(getattr(m, "content", ""), str) else "" for m in api_msgs)
    assert "别改 x" not in joined
    assert "修 bug" not in joined
