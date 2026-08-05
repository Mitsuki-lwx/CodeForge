"""Skill 系统单元测试。

覆盖：parser / loader / active / render / filter_tool_registry / LoadSkill / Agent 集成。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.skills.active import ActiveSkills
from core.skills.errors import SkillDependencyError
from core.skills.executor import filter_tool_registry
from core.skills.loader import SkillLoader
from core.skills.parser import (
    SkillParseError,
    parse_skill_file,
    substitute_arguments,
)
from core.skills.render import render_body
from core.skills.types import SkillDef, SkillMeta, SkillSource

# ── 工具：临时 Skill 目录 ──────────────────────────────────────────


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """创建含单个 Skill 的临时目录。"""
    skills_dir = tmp_path / ".codeforge" / "skills"
    skill_dir = skills_dir / "hello"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: hello\n"
        "description: A test skill\n"
        "mode: inline\n"
        "---\n"
        "Say hello to the user.\n",
        encoding="utf-8",
    )
    return skills_dir


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "body") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return d


# ── Parser ─────────────────────────────────────────────────────────


def test_parse_valid_skill(skill_dir: Path):
    p = skill_dir / "hello" / "SKILL.md"
    skill = parse_skill_file(p, SkillSource.PROJECT)
    assert skill.meta.name == "hello"
    assert skill.meta.description == "A test skill"
    assert skill.meta.mode == "inline"
    assert skill.prompt_body == "Say hello to the user."
    assert skill.source == SkillSource.PROJECT
    assert skill.source_path == p.parent


def test_parse_directory_flag(tmp_path: Path):
    d = _write_skill(tmp_path, "dirskill", "name: dirskill\ndescription: d\n")
    (d / "references").mkdir()
    skill = parse_skill_file(d / "SKILL.md", SkillSource.USER)
    assert skill.is_directory is True


def test_parse_flat_skill_not_directory(tmp_path: Path):
    d = _write_skill(tmp_path, "flat", "name: flat\ndescription: f\n")
    skill = parse_skill_file(d / "SKILL.md", SkillSource.USER)
    assert skill.is_directory is False


def test_parse_missing_opening(tmp_path: Path):
    f = tmp_path / "SKILL.md"
    f.write_text("no frontmatter here\n", encoding="utf-8")
    with pytest.raises(SkillParseError):
        parse_skill_file(f, SkillSource.PROJECT)


def test_parse_unclosed(tmp_path: Path):
    d = _write_skill(tmp_path, "bad", "name: bad\ndescription: b\n")
    f = d / "SKILL.md"
    f.write_text("---\nname: bad\n", encoding="utf-8")  # 无闭 ---
    with pytest.raises(SkillParseError):
        parse_skill_file(f, SkillSource.PROJECT)


def test_parse_invalid_yaml(tmp_path: Path):
    d = _write_skill(tmp_path, "bad", "name: [unclosed\n", body="x")
    with pytest.raises(SkillParseError):
        parse_skill_file(d / "SKILL.md", SkillSource.PROJECT)


def test_parse_non_dict_frontmatter(tmp_path: Path):
    d = _write_skill(tmp_path, "bad", "- item1\n- item2\n", body="x")
    with pytest.raises(SkillParseError):
        parse_skill_file(d / "SKILL.md", SkillSource.PROJECT)


def test_parse_missing_name(tmp_path: Path):
    d = _write_skill(tmp_path, "bad", "description: no name\n")
    with pytest.raises(SkillParseError):
        parse_skill_file(d / "SKILL.md", SkillSource.PROJECT)


def test_parse_invalid_name_format(tmp_path: Path):
    d = _write_skill(tmp_path, "bad", "name: HelloWorld\ndescription: d\n")
    with pytest.raises(SkillParseError):
        parse_skill_file(d / "SKILL.md", SkillSource.PROJECT)


def test_parse_invalid_mode_falls_back(tmp_path: Path):
    d = _write_skill(tmp_path, "bad", "name: bad\nmode: weird\ndescription: d\n")
    skill = parse_skill_file(d / "SKILL.md", SkillSource.PROJECT)
    assert skill.meta.mode == "inline"  # 非法 mode 回退 inline


def test_parse_fork_context(tmp_path: Path):
    d = _write_skill(
        tmp_path,
        "forky",
        "name: forky\nmode: fork\nfork_context: full\ndescription: f\n",
    )
    skill = parse_skill_file(d / "SKILL.md", SkillSource.PROJECT)
    assert skill.meta.mode == "fork"
    assert skill.meta.fork_context == "full"


def test_parse_allowed_tools(tmp_path: Path):
    d = _write_skill(
        tmp_path,
        "tools",
        "name: tools\ndescription: t\nallowed_tools: [Bash, Read]\n",
    )
    skill = parse_skill_file(d / "SKILL.md", SkillSource.PROJECT)
    assert skill.meta.allowed_tools == ["Bash", "Read"]


def test_parse_nonexistent_file(tmp_path: Path):
    with pytest.raises(SkillParseError):
        parse_skill_file(tmp_path / "missing.md", SkillSource.PROJECT)


# ── substitute_arguments ───────────────────────────────────────────


def test_substitute_with_args():
    out = substitute_arguments("Do: $ARGUMENTS", "the task")
    assert out == "Do: the task"


def test_substitute_empty_args():
    out = substitute_arguments("Do: $ARGUMENTS", "")
    assert out == "Do: "


def test_substitute_no_placeholder_appends():
    out = substitute_arguments("Just do it", "some param")
    assert "some param" in out


def test_substitute_multiple():
    out = substitute_arguments("$ARGUMENTS and $ARGUMENTS", "x")
    assert out == "x and x"


# ── Renderer ───────────────────────────────────────────────────────


def _skill_with(body: str, allowed=None, mode="inline"):
    return SkillDef(
        meta=SkillMeta(
            name="t",
            description="d",
            allowed_tools=allowed or [],
            mode=mode,
        ),
        prompt_body=body,
        source_path=Path("."),
        source=SkillSource.PROJECT,
    )


def test_render_no_args():
    skill = _skill_with("Do the thing")
    assert render_body(skill, "") == "Do the thing"


def test_render_replaces_placeholder():
    skill = _skill_with("Review $ARGUMENTS")
    out = render_body(skill, "auth")
    assert "Review auth" in out


def test_render_appends_user_request():
    skill = _skill_with("Do the thing")
    out = render_body(skill, "carefully")
    assert "carefully" in out
    assert "User Request" in out


def test_render_tool_hint():
    skill = _skill_with("Do", allowed=["Bash", "Read"])
    out = render_body(skill, "")
    assert "Bash" in out
    assert "Read" in out
    assert "only these tools" in out


# ── Loader ─────────────────────────────────────────────────────────


def test_loader_loads_project(tmp_path: Path):
    _write_skill(tmp_path / ".codeforge" / "skills", "a", "name: a\ndescription: A\n")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert "a" in loader.names()


def test_loader_user_dir(tmp_path: Path, monkeypatch):
    user_skills = tmp_path / "user" / ".codeforge" / "skills"
    _write_skill(user_skills, "u", "name: u\ndescription: U\n")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.chdir(tmp_path)  # 避免受真实 HOME 影响
    # 直接构造 user_dir
    loader = SkillLoader(tmp_path)
    loader._user_dir = user_skills  # 覆盖用户目录
    loader.load_all()
    assert "u" in loader.names()


def test_loader_project_overrides_user(tmp_path: Path):
    user_skills = tmp_path / "user" / "skills"
    proj_skills = tmp_path / ".codeforge" / "skills"
    _write_skill(user_skills, "dup", "name: dup\ndescription: user version\n")
    _write_skill(proj_skills, "dup", "name: dup\ndescription: project version\n")

    loader = SkillLoader(tmp_path)
    loader._user_dir = user_skills
    loader.load_all()

    skill = loader.get("dup")
    assert skill is not None
    assert skill.meta.description == "project version"
    assert loader.get_source_label("dup") == "project"


def test_loader_get_unknown_returns_none(tmp_path: Path):
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert loader.get("nope") is None


def test_loader_hot_reload(tmp_path: Path):
    d = _write_skill(
        tmp_path / ".codeforge" / "skills",
        "h",
        "name: h\ndescription: v1\n",
        "first body",
    )
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert loader.get("h").prompt_body == "first body"

    # 修改文件后不重启 → get 重读
    (d / "SKILL.md").write_text(
        "---\nname: h\ndescription: v2\n---\nsecond body\n", encoding="utf-8"
    )
    assert loader.get("h").prompt_body == "second body"


def test_loader_hot_reload_failure_falls_back(tmp_path: Path):
    d = _write_skill(
        tmp_path / ".codeforge" / "skills",
        "h",
        "name: h\ndescription: v1\n",
        "old body",
    )
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert loader.get("h").prompt_body == "old body"

    # 改成非法 frontmatter → 回退旧缓存
    (d / "SKILL.md").write_text("---\ninvalid: [yaml\n---\n", encoding="utf-8")
    skill = loader.get("h")
    assert skill is not None
    assert skill.prompt_body == "old body"  # 回退缓存


def test_loader_skips_bad_skill(tmp_path: Path):
    skills_dir = tmp_path / ".codeforge" / "skills"
    _write_skill(skills_dir, "good", "name: good\ndescription: G\n")
    _write_skill(skills_dir, "bad", "no name field\n")  # 解析失败

    loader = SkillLoader(tmp_path)
    loader.load_all()  # 不抛

    assert "good" in loader.names()
    assert "bad" not in loader.names()


def test_loader_reload(tmp_path: Path):
    skills_dir = tmp_path / ".codeforge" / "skills"
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert loader.names() == []

    _write_skill(skills_dir, "new", "name: new\ndescription: N\n")
    loader.reload()
    assert "new" in loader.names()


def test_loader_source_label(tmp_path: Path):
    proj = tmp_path / ".codeforge" / "skills"
    _write_skill(proj, "p", "name: p\ndescription: P\n")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert loader.get_source_label("p") == "project"
    assert loader.get_source_label("missing") == "unknown"


# ── ActiveSkills ───────────────────────────────────────────────────


def test_active_activate_and_snapshot():
    a = ActiveSkills()
    a.activate("x", "body-x")
    a.activate("y", "body-y")
    snap = a.snapshot()
    assert [e.name for e in snap] == ["x", "y"]
    assert snap[0].body == "body-x"


def test_active_duplicate_updates_in_place():
    a = ActiveSkills()
    a.activate("x", "v1")
    a.activate("y", "v1")
    a.activate("x", "v2")  # 覆盖 body，不新增条目
    snap = a.snapshot()
    assert len(snap) == 2
    assert snap[0].body == "v2"


def test_active_clear():
    a = ActiveSkills()
    a.activate("x", "body")
    a.clear()
    assert a.snapshot() == []
    assert a.names() == []


def test_active_names():
    a = ActiveSkills()
    a.activate("b", "1")
    a.activate("a", "2")
    assert a.names() == ["b", "a"]  # 保持激活顺序


# ── filter_tool_registry ───────────────────────────────────────────


class _FakeTool:
    def __init__(self, name: str, system: bool = False):
        self._name = name
        self.is_system_tool = system

    def name(self):
        return self._name


def _fake_registry(tools: list[_FakeTool]):
    from core.tool.registry import ToolRegistry

    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def test_filter_empty_allowed_returns_same():
    reg = _fake_registry([_FakeTool("Read"), _FakeTool("Bash")])
    out = filter_tool_registry(reg, [], "skill")
    assert out is reg


def test_filter_subset():
    reg = _fake_registry([_FakeTool("Read"), _FakeTool("Bash"), _FakeTool("Grep")])
    out = filter_tool_registry(reg, ["Read", "Grep"], "skill")
    names = {t.name() for t in out.list()}
    assert names == {"Read", "Grep"}


def test_filter_system_tool_passthrough():
    reg = _fake_registry([_FakeTool("Read"), _FakeTool("LoadSkill", system=True)])
    out = filter_tool_registry(reg, ["Read"], "skill")
    names = {t.name() for t in out.list()}
    assert "Read" in names
    assert "LoadSkill" in names  # 系统工具豁免


def test_filter_missing_tool_raises():
    reg = _fake_registry([_FakeTool("Read")])
    with pytest.raises(SkillDependencyError):
        filter_tool_registry(reg, ["NonExistent"], "skill")


def test_registry_definitions_filtered_returns_new_instance():
    reg = _fake_registry([_FakeTool("Read"), _FakeTool("LoadSkill", system=True)])
    out = reg.definitions_filtered(["Read"])
    assert out is not reg
    assert {t.name() for t in out.list()} == {"Read", "LoadSkill"}


# ── LoadSkill 工具 ─────────────────────────────────────────────────


def test_load_skill_tool_basic(skill_dir: Path):
    from core.tool.tools.load_skill import LoadSkillTool

    loader = SkillLoader(skill_dir.parent.parent)
    loader.load_all()

    class _FakeAgent:
        def __init__(self):
            self.activated = {}

        def activate_skill(self, name, body):
            self.activated[name] = body

    agent = _FakeAgent()
    tool = LoadSkillTool()
    tool.set_loader(loader)
    tool.set_agent(agent)

    from core.tool.context import ExecutionContext

    ctx = ExecutionContext(cwd=Path("."), session_id="test")
    result = asyncio.run(tool.execute(ctx, {"name": "hello"}))
    assert result.success is True
    assert "hello" in agent.activated
    assert "Say hello" in agent.activated["hello"]


def test_load_skill_unknown(skill_dir: Path):
    from core.tool.tools.load_skill import LoadSkillTool

    loader = SkillLoader(skill_dir.parent.parent)
    loader.load_all()
    tool = LoadSkillTool()
    tool.set_loader(loader)

    class _FakeAgent:
        def activate_skill(self, name, body):
            raise AssertionError("should not activate")

    tool.set_agent(_FakeAgent())

    from core.tool.context import ExecutionContext

    ctx = ExecutionContext(cwd=Path("."), session_id="test")
    result = asyncio.run(tool.execute(ctx, {"name": "nope"}))
    assert result.success is False
    assert "Unknown skill" in result.error


def test_load_skill_not_initialized():
    from core.tool.tools.load_skill import LoadSkillTool

    tool = LoadSkillTool()
    from core.tool.context import ExecutionContext

    ctx = ExecutionContext(cwd=Path("."), session_id="test")
    result = asyncio.run(tool.execute(ctx, {"name": "x"}))
    assert result.success is False
    assert "not properly initialized" in result.error


def test_load_skill_is_system():
    from core.tool.tools.load_skill import LoadSkillTool

    tool = LoadSkillTool()
    assert tool.is_system_tool is True
    assert tool.name() == "LoadSkill"
    assert tool.category() == "skill"
    assert tool.is_read_only() is True


def test_load_skill_requires_name(skill_dir: Path):
    from core.tool.tools.load_skill import LoadSkillTool

    loader = SkillLoader(skill_dir.parent.parent)
    loader.load_all()
    tool = LoadSkillTool()
    tool.set_loader(loader)
    tool.set_agent(_DummyAgent())

    from core.tool.context import ExecutionContext

    ctx = ExecutionContext(cwd=Path("."), session_id="test")
    result = asyncio.run(tool.execute(ctx, {}))
    assert result.success is False
    assert "required" in result.error


class _DummyAgent:
    def activate_skill(self, name, body):
        pass


# ── Agent 集成 ─────────────────────────────────────────────────────


def test_agent_activate_skill_injects_to_env():
    from core.agent.runtime import SessionRuntime

    # 直接验证 ActiveSkills 挂载到 runtime
    rt = SessionRuntime()
    rt.active_skills.activate("test", "SOP body here")
    snap = rt.active_skills.snapshot()
    assert len(snap) == 1
    assert snap[0].name == "test"
    assert snap[0].body == "SOP body here"


def test_runtime_reset_clears_skills():
    from core.agent.runtime import SessionRuntime
    from core.context_compression.state import SessionContext

    rt = SessionRuntime()
    rt.active_skills.activate("x", "body")
    rt.reset_for_new_session(
        SessionContext(session_id="new", session_dir="d", spill_dir="s")
    )
    assert rt.active_skills.snapshot() == []


def test_agent_clear_active_skills():
    from core.agent.runtime import SessionRuntime

    rt = SessionRuntime()
    rt.active_skills.activate("x", "body")
    rt.active_skills.clear()
    assert rt.active_skills.names() == []


# ── 命令集成 ───────────────────────────────────────────────────────


class _RecorderUI:
    """记录 println / inject_and_send / append_assistant_message 的 UI 桩。"""

    def __init__(self):
        self.printlns = []
        self.injects = []
        self.errors = []
        self.appended = []

    def println(self, msg):
        self.printlns.append(msg)

    def error(self, msg):
        self.errors.append(msg)

    async def inject_and_send(self, label, preset):
        self.injects.append((label, preset))

    async def append_assistant_message(self, text):
        self.appended.append(text)

    def idle(self):
        return True


def _make_skill_env(tmp_path: Path):
    """构建 loader + executor + agent + registry 的最小环境。"""
    from core.agent.runtime import SessionRuntime
    from core.commands.builtins import register_builtins
    from core.commands.registry import Registry
    from core.commands.skill_register import register_skills_as_commands
    from core.skills.executor import SkillExecutor
    from core.tool.registry import ToolRegistry

    _write_skill(
        tmp_path / ".codeforge" / "skills",
        "myskill",
        "name: myskill\ndescription: A skill\nmode: inline\n",
        "Do $ARGUMENTS carefully",
    )

    loader = SkillLoader(tmp_path)
    loader.load_all()

    class _Agent:
        def __init__(self):
            self.activated = {}

        def activate_skill(self, name, body):
            self.activated[name] = body

    agent = _Agent()
    executor = SkillExecutor(loader, SessionRuntime(), ToolRegistry(), None, tmp_path)
    executor.set_agent(agent)

    reg = Registry()
    register_builtins(reg)
    register_skills_as_commands(reg, loader, executor)

    return reg, loader, executor, agent


def test_skill_registered_as_command(tmp_path: Path):
    reg, *_ = _make_skill_env(tmp_path)
    cmd = reg.lookup("myskill")
    assert cmd is not None
    assert "[skill]" in cmd.description
    assert cmd.kind.value == "prompt"


@pytest.mark.asyncio
async def test_skill_inline_dispatch_activates(tmp_path: Path):
    reg, _, _, agent = _make_skill_env(tmp_path)
    ui = _RecorderUI()
    cmd = reg.lookup("myskill")

    await cmd.handler(ui, "")

    # SOP 已激活到 agent
    assert "myskill" in agent.activated
    assert "Do  carefully" in agent.activated["myskill"]
    # 消息已注入（触发 Agent 回合）
    assert len(ui.injects) == 1
    assert ui.injects[0][0] == "/myskill"
    assert "Do" in ui.injects[0][1]


@pytest.mark.asyncio
async def test_skill_inline_dispatch_with_args(tmp_path: Path):
    reg, _, _, agent = _make_skill_env(tmp_path)
    ui = _RecorderUI()
    cmd = reg.lookup("myskill")

    await cmd.handler(ui, "test the login")

    assert "Do test the login carefully" in agent.activated["myskill"]


def test_skill_command_register_twice_no_duplicate(tmp_path: Path):
    from core.commands.skill_register import register_skills_as_commands

    reg, loader, executor, _ = _make_skill_env(tmp_path)
    # 第二次注册 → 先清理旧的，再重新注册
    register_skills_as_commands(reg, loader, executor)

    matches = [c for c in reg.visible() if c.name == "myskill"]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_skill_list_command(tmp_path: Path):
    from core.commands.builtin_skill import handle_skill

    _, loader, executor, _ = _make_skill_env(tmp_path)
    ui = _RecorderUI()

    await handle_skill(ui, "list", catalog=loader, executor=executor)

    joined = "\n".join(ui.printlns)
    assert "myskill" in joined
    assert "A skill" in joined
    assert "project" in joined


@pytest.mark.asyncio
async def test_skill_info_command(tmp_path: Path):
    from core.commands.builtin_skill import handle_skill

    _, loader, executor, _ = _make_skill_env(tmp_path)
    ui = _RecorderUI()

    await handle_skill(ui, "info myskill", catalog=loader, executor=executor)

    joined = "\n".join(ui.printlns)
    assert "Name:" in joined and "myskill" in joined
    assert "Mode:" in joined and "inline" in joined
    assert "Path:" in joined
