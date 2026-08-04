"""命令框架单测：注册/解析/冲突/内置命令。"""

from __future__ import annotations

import pytest

from core.commands import (
    Kind,
    Registry,
    parse_line,
    register_builtins,
)
from core.commands.registry import CommandRegistryError
from core.commands.types import Command
from core.commands.ui import NopUI
from core.permissions.modes import PermissionMode


def _cmd(name: str, kind: Kind = Kind.LOCAL, aliases=None, hidden=False):
    async def _h(ui, args=""):
        pass

    return Command(
        name=name,
        description=f"desc {name}",
        kind=kind,
        handler=_h,
        aliases=aliases or [],
        hidden=hidden,
    )


# ── 注册中心 ───────────────────────────────────────────────────────


def test_register_ok_and_lookup():
    reg = Registry()
    reg.register(_cmd("plan", Kind.UI))
    assert reg.lookup("plan") is not None
    assert reg.lookup("/plan") is not None
    assert reg.lookup("PLAN") is not None
    assert reg.lookup("/PLAN") is not None


def test_register_duplicate_name_raises():
    reg = Registry()
    reg.register(_cmd("plan", Kind.UI))
    with pytest.raises(CommandRegistryError) as e:
        reg.register(_cmd("plan", Kind.LOCAL))
    assert "plan" in str(e.value)


def test_register_duplicate_alias_raises():
    reg = Registry()
    reg.register(_cmd("help", aliases=["h"]))
    with pytest.raises(CommandRegistryError) as e:
        reg.register(_cmd("home", aliases=["h"]))
    assert "h" in str(e.value)


def test_register_alias_collides_with_existing_name():
    reg = Registry()
    reg.register(_cmd("exit"))
    with pytest.raises(CommandRegistryError):
        reg.register(_cmd("quit", aliases=["exit"]))


def test_visible_sorted():
    reg = Registry()
    reg.register(_cmd("zebra"))
    reg.register(_cmd("apple"))
    reg.register(_cmd("mango"))
    assert [c.name for c in reg.visible()] == ["apple", "mango", "zebra"]


def test_visible_excludes_hidden_but_lookup_hits():
    reg = Registry()
    reg.register(_cmd("secret", hidden=True))
    assert all(c.name != "secret" for c in reg.visible())
    assert reg.lookup("secret") is not None


def test_prefix_match():
    reg = Registry()
    reg.register(_cmd("session"))
    reg.register(_cmd("status"))
    reg.register(_cmd("memory"))
    hits = reg.prefix_match("/s")
    assert [c.name for c in hits] == ["session", "status"]
    assert reg.prefix_match("") == reg.visible()
    assert reg.prefix_match("/m")[0].name == "memory"


def test_register_requires_lowercase():
    reg = Registry()
    with pytest.raises(CommandRegistryError):
        reg.register(_cmd("HELP"))


# ── 解析器 ─────────────────────────────────────────────────────────


def test_parse_empty_and_plain():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("hello world") is None


def test_parse_slash_only():
    assert parse_line("/") is not None
    assert parse_line("/").name == ""


def test_parse_name_and_args():
    call = parse_line("/plan")
    assert call is not None
    assert call.name == "plan"
    assert call.args == ""


def test_parse_case_insensitive():
    call = parse_line("/PLAN")
    assert call.name == "plan"


def test_parse_review_args():
    call = parse_line("/review 注意安全")
    assert call.name == "review"
    assert call.args == "注意安全"


def test_parse_trailing_space():
    call = parse_line("/help  ")
    assert call.name == "help"
    assert call.args == ""


# ── 内置命令注册 ───────────────────────────────────────────────────


def _builtin_reg() -> Registry:
    reg = Registry()
    register_builtins(reg)
    return reg


def test_register_builtins_all_registered():
    reg = _builtin_reg()
    names = [c.name for c in reg.visible()]
    expected = [
        "clear",
        "compact",
        "do",
        "exit",
        "help",
        "memory",
        "permission",
        "plan",
        "resume",
        "session",
        "status",
    ]
    assert names == expected  # 字典序且 11 条（/review 改为 Skill 提供）


def test_register_builtins_no_collision():
    _builtin_reg()  # 不抛即通过


def test_builtin_aliases_work():
    reg = _builtin_reg()
    assert reg.lookup("compress") is not None  # /compact 别名
    assert reg.lookup("notes") is not None  # /memory 别名
    assert reg.lookup("mode") is not None  # /permission 别名


# ── handler 在 NopUI 上可执行 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_all_handlers_run_on_nop_ui():
    reg = _builtin_reg()
    for cmd in reg.visible():
        await cmd.handler(NopUI(), "")


class _RecordingUI(NopUI):
    def __init__(self):
        self.printlns: list[str] = []
        self.errors: list[str] = []
        self.modes: list[PermissionMode] = []
        self.injects: list[tuple[str, str]] = []
        self.compact_calls = 0
        self._idle = True

    def println(self, msg):
        self.printlns.append(msg)

    def error(self, msg):
        self.errors.append(msg)

    def set_mode(self, mode):
        self.modes.append(mode)

    async def inject_and_send(self, label, preset):
        self.injects.append((label, preset))

    async def force_compact(self):
        self.compact_calls += 1

    def idle(self):
        return self._idle


@pytest.mark.asyncio
async def test_status_prints_all_keys():
    from core.commands.builtin_local import handle_status

    ui = _RecordingUI()
    await handle_status(ui, "")
    assert ui.printlns
    text = ui.printlns[0]
    for key in ("Mode:", "Tokens:", "Tools:", "Memories:", "Model:", "Directory:"):
        assert key in text


@pytest.mark.asyncio
async def test_do_sets_mode_and_injects():
    from core.commands.builtin_prompt import handle_do

    ui = _RecordingUI()
    await handle_do(ui, "")
    assert ui.modes == [PermissionMode.DEFAULT]
    assert ui.injects and ui.injects[0][0] == "/do"


@pytest.mark.asyncio
async def test_plan_sets_mode_and_prints():
    from core.commands.builtin_ui import handle_plan

    ui = _RecordingUI()
    await handle_plan(ui, "")
    assert ui.modes == [PermissionMode.PLAN]
    assert any("PLAN" in p for p in ui.printlns)


@pytest.mark.asyncio
async def test_compact_calls_force_compact():
    from core.commands.builtin_ui import handle_compact

    ui = _RecordingUI()
    await handle_compact(ui, "")
    assert ui.compact_calls == 1


# ── dispatch_slash 分流（最小 fake app）────────────────────────────

import io

from rich.console import Console

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from tui.app import CodeForgeApp


class _FakeChecker:
    plan_file_path = None


class _FakeRegistry:
    def count(self):
        return 3


class _FakeAgent:
    def __init__(self):
        self.permission_mode = PermissionMode.DEFAULT
        self._total_usage = {"input_tokens": 0, "output_tokens": 0}
        self._registry = _FakeRegistry()
        self._plan_path = None
        self._permission_checker = _FakeChecker()
        self._runtime = None
        self._conversation = None

    def set_permission_mode(self, m):
        self.permission_mode = m

    @property
    def plan_mode(self):
        return self.permission_mode == PermissionMode.PLAN


def _make_app() -> CodeForgeApp:
    reg = Registry()
    register_builtins(reg)
    buf = io.StringIO()
    app = CodeForgeApp(
        console=Console(file=buf),
        agent=_FakeAgent(),
        session=None,
        conversation=ConversationManager(),
        runtime=None,
        provider=ProviderConfig(name="x", protocol="anthropic", model="m", api_key="k"),
        mcp_pool=None,
        workspace=".",
        notes=None,
        writer=None,
        cmd_registry=reg,
    )
    return app


@pytest.mark.asyncio
async def test_dispatch_non_slash_returns_false():
    app = _make_app()
    assert await app.dispatch_slash("hello") is False


@pytest.mark.asyncio
async def test_dispatch_unknown_guides_help():
    app = _make_app()
    assert await app.dispatch_slash("/bogus") is True
    assert "/help" in app.console.file.getvalue()


@pytest.mark.asyncio
async def test_dispatch_status_runs_local():
    app = _make_app()
    assert await app.dispatch_slash("/status") is True
    out = app.console.file.getvalue()
    for key in ("Mode:", "Tokens:", "Tools:", "Memories:", "Model:", "Directory:"):
        assert key in out


@pytest.mark.asyncio
async def test_dispatch_help_lists_all_builtins():
    app = _make_app()
    await app.dispatch_slash("/help")
    out = app.console.file.getvalue()
    for name in (
        "clear",
        "compact",
        "do",
        "exit",
        "help",
        "memory",
        "permission",
        "plan",
        "resume",
        "session",
        "status",
    ):
        assert f"/{name}" in out


@pytest.mark.asyncio
async def test_dispatch_case_insensitive():
    app = _make_app()
    assert await app.dispatch_slash("/Help") is True
    assert "/help" in app.console.file.getvalue()


@pytest.mark.asyncio
async def test_dispatch_ui_blocked_when_busy():
    app = _make_app()
    app.agent_running = True
    await app.dispatch_slash("/compact")
    assert "请等待当前任务完成" in app.console.file.getvalue()


@pytest.mark.asyncio
async def test_dispatch_local_allowed_when_busy():
    app = _make_app()
    app.agent_running = True
    assert await app.dispatch_slash("/status") is True  # LOCAL 不拒绝


@pytest.mark.asyncio
async def test_dispatch_no_arg_command_rejects_args():
    app = _make_app()
    await app.dispatch_slash("/help xx")
    assert "不接受参数" in app.console.file.getvalue()


@pytest.mark.asyncio
async def test_dispatch_review_not_builtin():
    # /review 不再是内置命令 —— 由 Skill 系统提供（见 test_skills.py）
    app = _make_app()
    await app.dispatch_slash("/review 注意安全")
    assert "/help" in app.console.file.getvalue()  # 未命中 → 引导 /help


@pytest.mark.asyncio
async def test_dispatch_exit_raises_systemexit():
    app = _make_app()
    with pytest.raises(SystemExit):
        await app.dispatch_slash("/exit")
