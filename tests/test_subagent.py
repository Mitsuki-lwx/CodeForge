"""SubAgent 角色系统 + Fork 辅助 + 工具过滤 单元测试。

覆盖：roles 解析 / catalog 三层加载 / fork 消息构造 / 工具过滤多层防线。
"""

from __future__ import annotations

from pathlib import Path

from conversation.message import Message, MessageRole
from core.agent.fork import (
    FORK_BOILERPLATE,
    FORK_BOILERPLATE_TAG,
    build_forked_messages,
    is_fork_context,
)
from core.agent.role_loader import Catalog, builtin_roles, load_catalog
from core.agent.roles import (
    AgentRole,
    Source,
    parse_role_bytes,
    parse_role_file,
)
from core.tool.filter import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    FilterParams,
    apply_agent_tool_filter,
    is_mcp_or_skill,
)

# ── roles 解析 ─────────────────────────────────────────────────────


def test_parse_role_full():
    md = b"""---
name: Explore
description: read-only exploration agent
tools:
  - read_file
disallowedTools:
  - write_file
  - edit_file
model: haiku
maxTurns: 30
permissionMode: dontAsk
background: true
---

You are a file search expert.
"""
    role = parse_role_bytes(md, "test.md", Source.PROJECT)
    assert role is not None
    assert role.name == "Explore"
    assert role.description == "read-only exploration agent"
    assert role.tools == ["read_file"]
    assert role.disallowed_tools == ["write_file", "edit_file"]
    assert role.model == "haiku"
    assert role.max_turns == 30
    assert role.dont_ask is True
    assert role.background is True
    assert "file search expert" in role.system_prompt
    assert not role.is_fork()


def test_parse_role_required_only():
    md = b"""---
name: simple
description: minimal
---
body
"""
    role = parse_role_bytes(md, "t.md", Source.PROJECT)
    assert role is not None
    assert role.model == "inherit"
    assert role.max_turns == 0
    assert not role.dont_ask


def test_parse_role_invalid_model_fallback():
    md = b"""---
name: bad
description: bad model
model: gpt-4
---
body
"""
    role = parse_role_bytes(md, "t.md", Source.USER)
    assert role is not None
    assert role.model == "inherit"  # fallback


def test_parse_role_invalid_mode_fallback():
    md = b"""---
name: bad
description: bad mode
permissionMode: weirdMode
---
body
"""
    role = parse_role_bytes(md, "t.md", Source.USER)
    assert role is not None
    assert role.permission_mode.value == "default"  # fallback


def test_parse_role_missing_name_returns_none():
    role = parse_role_bytes(b"---\ndescription: hi\n---\nbody", "t.md", Source.USER)
    assert role is None


def test_parse_role_missing_description_returns_none():
    role = parse_role_bytes(b"---\nname: hello\n---\nbody", "t.md", Source.USER)
    assert role is None


def test_parse_role_invalid_yaml_returns_none():
    role = parse_role_bytes(b"---\ninvalid: [yaml\n---\nbody", "t.md", Source.USER)
    assert role is None


def test_parse_role_file(tmp_path: Path):
    p = tmp_path / "role.md"
    p.write_bytes(b"---\nname: x\ndescription: y\n---\nbody")
    role = parse_role_file(str(p), Source.PROJECT)
    assert role is not None
    assert role.name == "x"


def test_fork_role():
    role = AgentRole(name="__fork__", description="fork")
    assert role.is_fork()


# ── catalog 三层加载 ───────────────────────────────────────────────


def test_builtin_roles_load():
    roles = builtin_roles()
    names = {r.name for r in roles}
    assert names == {"general-purpose", "Explore", "Plan", "Coder"}
    explore = next(r for r in roles if r.name == "Explore")
    assert explore.disallowed_tools == ["write_file", "edit_file"]
    assert explore.model == "haiku"
    coder = next(r for r in roles if r.name == "Coder")
    assert coder.isolation is True  # 隔离子代理 → 可安全并行


def test_catalog_resolve_and_list():
    cat = Catalog()
    cat._add_all(builtin_roles())
    assert cat.resolve("Explore") is not None
    assert cat.resolve("nonexistent") is None
    assert len(cat.list()) == 4
    assert cat.fork_role().is_fork()


def test_load_catalog_priority_override(tmp_path: Path, monkeypatch):
    # 项目级覆盖内置 Explore
    proj_dir = tmp_path / ".codeforge" / "agents"
    proj_dir.mkdir(parents=True)
    (proj_dir / "explore.md").write_bytes(
        b"---\nname: Explore\ndescription: project override\nmodel: sonnet\n---\nproj body\n"
    )

    # 用户级覆盖（但项目级更高优先级）
    home = tmp_path / "home"
    home.mkdir()
    user_dir = home / ".codeforge" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "explore.md").write_bytes(
        b"---\nname: Explore\ndescription: user override\nmodel: haiku\n---\nuser body\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    cat = load_catalog(str(tmp_path))
    explore = cat.resolve("Explore")
    assert explore is not None
    # 项目级优先（description / model 都来自项目级）
    assert explore.description == "project override"
    assert explore.model == "sonnet"
    assert explore.source == Source.PROJECT


def test_load_catalog_user_level(tmp_path: Path, monkeypatch):
    # 只有用户级有 Explore 覆盖
    home = tmp_path / "home"
    home.mkdir()
    user_dir = home / ".codeforge" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "explore.md").write_bytes(
        b"---\nname: Explore\ndescription: user only\n---\nbody\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    cat = load_catalog(str(tmp_path))
    explore = cat.resolve("Explore")
    assert explore is not None
    assert explore.description == "user only"
    assert explore.source == Source.USER


def test_load_catalog_invalid_file_skipped(tmp_path: Path):
    proj_dir = tmp_path / ".codeforge" / "agents"
    proj_dir.mkdir(parents=True)
    (proj_dir / "bad.md").write_bytes(b"this is not a valid role file\n")
    (proj_dir / "good.md").write_bytes(
        b"---\nname: good\ndescription: ok\n---\nbody\n"
    )
    cat = load_catalog(str(tmp_path))
    assert cat.resolve("good") is not None  # 好文件被加载
    assert cat.resolve("bad") is None  # 坏文件被跳过


def test_load_catalog_empty_dir(tmp_path: Path):
    cat = load_catalog(str(tmp_path))
    assert len(cat.list()) == 4  # 只有内置（含 Coder）


# ── Fork 辅助 ──────────────────────────────────────────────────────


def test_build_forked_empty_parent():
    msgs = build_forked_messages([], "do the thing")
    assert len(msgs) == 1
    assert msgs[0].role == MessageRole.USER
    assert FORK_BOILERPLATE_TAG in msgs[0].content
    assert "do the thing" in msgs[0].content


def test_build_forked_normal_parent():
    parent = [
        Message(role=MessageRole.USER, content="hi"),
        Message(role=MessageRole.ASSISTANT, content="hello"),
    ]
    msgs = build_forked_messages(parent, "task")
    assert len(msgs) == 3
    assert msgs[-1].content.startswith(FORK_BOILERPLATE)


def test_build_forked_unpaired_tool_use():
    parent = [
        Message(role=MessageRole.USER, content="run it"),
        Message(
            role=MessageRole.ASSISTANT, content="", tool_use_id="tu1",
            tool_name="bash", tool_input={"command": "ls"},
        ),
    ]
    msgs = build_forked_messages(parent, "fork task")
    # user + assistant(tool_use) + placeholder tool_result + fork user
    assert len(msgs) == 4
    assert msgs[2].role == MessageRole.USER
    assert msgs[2].tool_use_id == "tu1"
    assert msgs[2].content == "[forked, skipped]"
    assert FORK_BOILERPLATE_TAG in msgs[3].content


def test_is_fork_context():
    parent = [Message(role=MessageRole.USER, content="plain")]
    assert is_fork_context(parent) is False
    forked = build_forked_messages(parent, "task")
    assert is_fork_context(forked) is True


# ── 工具过滤 ───────────────────────────────────────────────────────


def test_agent_disallowed_constant():
    assert ALL_AGENT_DISALLOWED_TOOLS == ["Agent"]


def test_async_allowed_constant():
    for t in ("read_file", "write_file", "edit_file", "glob", "grep", "bash",
              "load_skill", "install_skill"):
        assert t in ASYNC_AGENT_ALLOWED_TOOLS


def test_filter_default_removes_agent():
    tools = ["read_file", "write_file", "grep", "Agent", "bash", "TaskList"]
    result = apply_agent_tool_filter(FilterParams(all=tools))
    assert "Agent" not in result
    assert len(result) == 5


def test_filter_background_whitelist():
    tools = ["read_file", "write_file", "grep", "Agent", "bash", "TaskList"]
    result = apply_agent_tool_filter(FilterParams(all=tools, background=True))
    assert "read_file" in result
    assert "bash" in result
    assert "TaskList" not in result
    assert "Agent" not in result


def test_filter_mcp_kept_in_background():
    tools = ["read_file", "bash", "mcp__context7"]
    result = apply_agent_tool_filter(FilterParams(all=tools, background=True))
    assert "mcp__context7" in result


def test_filter_blacklist():
    tools = ["read_file", "bash", "write_file"]
    result = apply_agent_tool_filter(
        FilterParams(all=tools, disallowed=["bash"])
    )
    assert "bash" not in result
    assert "read_file" in result


def test_filter_allowlist():
    tools = ["read_file", "grep", "bash", "write_file"]
    result = apply_agent_tool_filter(
        FilterParams(all=tools, allowed=["read_file", "grep"])
    )
    assert result == ["read_file", "grep"]


def test_filter_combined():
    tools = ["read_file", "write_file", "grep", "bash"]
    result = apply_agent_tool_filter(
        FilterParams(
            all=tools,
            background=True,
            disallowed=["bash"],
            allowed=["read_file", "write_file", "grep", "bash"],
        )
    )
    # 后台白名单 ∩ 黑名单(去bash)
    assert "read_file" in result
    assert "grep" in result
    assert "bash" not in result


def test_is_mcp_or_skill():
    assert is_mcp_or_skill("mcp__filesystem") is True
    assert is_mcp_or_skill("read_file") is False
