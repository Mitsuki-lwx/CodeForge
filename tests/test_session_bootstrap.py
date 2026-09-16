"""装配等价性测试 —— 新建与恢复必须产出同样的能力集。

背景：`CodeForgeApp.resume_session` 曾经手搓一个裸 Agent（只有
`get_default_registry()`，不传 hooks、不建会话状态、不注入 skill catalog、
不设 loop），导致恢复出来的会话能力弱于新会话。两条路径现在共用
`core.agent.bootstrap.build_session`，本文件锁死这个等价性。

断言的是**工具名集合相等**（不是计数相等）——计数相等可能掩盖
「少一个多一个」的替换型回归。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent.bootstrap import ReuseContext, build_session
from core.agent.loop import ReactLoop
from core.permissions.modes import PermissionMode


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        protocol="openai",
        model="gpt-4o",
        api_key="sk-test-not-a-real-key",
    )


def _tool_names(registry) -> set[str]:
    return {t.name() for t in registry.list()}


async def _build(workspace: Path, **kwargs):
    """装配一个会话；调用方负责 close。"""
    return await build_session(
        provider=_provider(), workspace=workspace, **kwargs
    )


# ── 核心：恢复与新建的能力集一致 ──────────────────────────────────


async def test_resume_matches_new_tool_set(tmp_path):
    """恢复会话与新会话的注册工具名集合必须完全相等。"""
    fresh = await _build(tmp_path)
    try:
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
        )
        try:
            assert _tool_names(resumed.registry) == _tool_names(fresh.registry)
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_resume_wires_hooks(tmp_path):
    """恢复会话必须挂上 HookRunner —— 此前恢复路径完全没有 hook。"""
    fresh = await _build(tmp_path)
    try:
        assert fresh.agent._hooks is not None
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
        )
        try:
            assert resumed.hook_runner is not None
            assert resumed.agent._hooks is not None
            assert resumed.runtime.hook_runner is not None
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_resume_wires_state_store(tmp_path):
    """恢复会话必须建会话状态存储 —— 此前恢复后状态工具会消失。"""
    fresh = await _build(tmp_path)
    try:
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
        )
        try:
            assert resumed.agent._state_store is not None
            # 三个状态工具必须在册（spec_session_state 的写入通道）
            assert {"SetGoal", "AddTodo", "AddConstraint"} <= _tool_names(
                resumed.registry
            )
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_resume_wires_skill_catalog(tmp_path):
    """恢复会话必须注入 skill catalog，且内容与新会话一致。"""
    fresh = await _build(tmp_path)
    try:
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
        )
        try:
            assert resumed.agent._skill_catalog == fresh.agent._skill_catalog
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_resume_respects_loop_spec(tmp_path):
    """恢复会话必须同样应用 loop 配置 —— 此前恢复会静默回落默认循环。"""
    fresh = await _build(tmp_path, loop_spec="react")
    try:
        resumed = await _build(
            tmp_path,
            loop_spec="react",
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
        )
        try:
            assert isinstance(resumed.agent._loop, ReactLoop)
            assert type(resumed.agent._loop) is type(fresh.agent._loop)
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_resume_permission_mode_matches(tmp_path):
    """恢复会话的初始权限模式与新会话一致。"""
    fresh = await _build(tmp_path)
    try:
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
        )
        try:
            assert resumed.agent.permission_mode == fresh.agent.permission_mode
            assert resumed.agent.permission_mode == PermissionMode.DEFAULT
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_agent_identity_is_main_on_both_paths(tmp_path):
    """两条路径的主 Agent 身份 id 都是 main。

    此前恢复路径把 session_id 当作 agent id，导致同一会话在
    新建与恢复下的 trace/span 归属不同（子 Agent 是 `{id}-sub`）。
    """
    fresh = await _build(tmp_path)
    try:
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
        )
        try:
            assert fresh.agent._exec_ctx.session_id == "main"
            assert resumed.agent._exec_ctx.session_id == "main"
        finally:
            resumed.close()
    finally:
        fresh.close()


# ── 无头模式 ──────────────────────────────────────────────────────


async def test_headless_sets_dont_ask(tmp_path):
    """无头模式放行 ask 级决策（与既有 --task 行为一致）。"""
    bundle = await _build(tmp_path, headless=True)
    try:
        assert bundle.agent._dont_ask is True
    finally:
        bundle.close()


async def test_interactive_does_not_set_dont_ask(tmp_path):
    """交互模式不设 dont_ask —— HITL 弹窗必须保留。"""
    bundle = await _build(tmp_path, headless=False)
    try:
        assert bundle.agent._dont_ask is False
    finally:
        bundle.close()


# ── 资源复用（同进程内切换会话）───────────────────────────────────


async def test_reuse_keeps_task_manager_identity(tmp_path):
    """复用 task_mgr：换掉它会让后台通知消费协程失去订阅。"""
    fresh = await _build(tmp_path)
    try:
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
            reuse=ReuseContext(
                mcp_pool=fresh.mcp_pool,
                task_mgr=fresh.task_mgr,
                wt_manager=fresh.wt_manager,
                notes=fresh.notes,
            ),
        )
        try:
            assert resumed.task_mgr is fresh.task_mgr
            assert resumed.mcp_pool is fresh.mcp_pool
            assert resumed.wt_manager is fresh.wt_manager
            assert resumed.notes is fresh.notes
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_reuse_skips_worktree_rescan(tmp_path):
    """复用 wt_manager 时不再重复上报 worktree 恢复。"""
    fresh = await _build(tmp_path)
    try:
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=ConversationManager(),
            reuse=ReuseContext(wt_manager=fresh.wt_manager),
        )
        try:
            assert not any("Worktree" in n for n in resumed.notices)
        finally:
            resumed.close()
    finally:
        fresh.close()


# ── 会话目录解析 ──────────────────────────────────────────────────


async def test_resume_uses_given_session_dir(tmp_path):
    """恢复时沿用给定会话目录，不新建。"""
    fresh = await _build(tmp_path)
    try:
        target = Path(fresh.runtime.session.session_dir)
        resumed = await _build(
            tmp_path, session_dir=target, conversation=ConversationManager()
        )
        try:
            assert Path(resumed.runtime.session.session_dir) == target
            assert resumed.runtime.session.session_id == target.name
        finally:
            resumed.close()
    finally:
        fresh.close()


async def test_new_session_creates_own_dir(tmp_path):
    """不传 session_dir 时新建独立会话目录。"""
    a = await _build(tmp_path)
    b = await _build(tmp_path)
    try:
        assert a.runtime.session.session_id != b.runtime.session.session_id
    finally:
        a.close()
        b.close()


async def test_conversation_callbacks_rebound_on_resume(tmp_path):
    """恢复时把既有对话的回调重绑到新 writer，保证新消息能落盘。"""
    fresh = await _build(tmp_path)
    try:
        restored = ConversationManager()
        resumed = await _build(
            tmp_path,
            session_dir=fresh.runtime.session.session_dir,
            conversation=restored,
        )
        try:
            assert resumed.conversation is restored
            # 新消息应写入新会话目录的 JSONL
            restored.add_user_message("hello after resume")
            text = Path(resumed.writer.path).read_text(encoding="utf-8")
            assert "hello after resume" in text
        finally:
            resumed.close()
    finally:
        fresh.close()


# ── 装配参数校验 ──────────────────────────────────────────────────


async def test_conversation_without_session_dir_rejected(tmp_path):
    """只给 conversation 不给 session_dir 应报错，避免对话与目录错配。"""
    with pytest.raises(ValueError, match="session_dir"):
        await _build(tmp_path, conversation=ConversationManager())
