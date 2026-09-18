"""Hook 系统单元测试：loader / validate / runner / inject / http / subagent / agent 集成 / /hooks 命令。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conversation.manager import ConversationManager
from conversation.message import MessageRole
from core.agent import Agent
from core.hooks import HookRunner, load_hooks_config
from core.hooks.events import HookContext
from core.hooks.inject import InjectionStore
from core.hooks.rules import HookAction, HookRule
from core.tool import ExecutionContext, ToolRegistry
from llm.stream_events import CompletionDone, TextChunk, ToolUse
from tests.test_agent import MockLLMClient

# ── Helpers ─────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rule(name="r", event="turn_start", action_type="command", command="", **kw):
    action_kwargs = kw.pop("action_kwargs", {})
    return HookRule(
        name=name,
        event=event,
        action=HookAction(type=action_type, command=command, **action_kwargs),
        **kw,
    )


def _ctx(event="turn_start", **kw):
    return HookContext(event=event, **kw)


class _CaptureClient(MockLLMClient):
    """捕获每次 stream_chat 收到的 system_prompt 与 system_blocks 内容。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.captured: list[str] = []

    async def stream_chat(
        self, messages, system_prompt="", tools=None, system_blocks=None
    ):
        if system_prompt:
            self.captured.append(system_prompt)
        if system_blocks is not None:
            for cb in system_blocks.cached:
                self.captured.append(cb.content)
            for ub in system_blocks.uncached:
                self.captured.append(ub.content)
        async for event in super().stream_chat(
            messages, system_prompt, tools, system_blocks
        ):
            yield event


# ── Loader ──────────────────────────────────────────────────────────


def test_loader_project_load(tmp_path):
    _write(
        tmp_path / ".codeforge" / "hooks.yaml",
        """rules:
  - name: a
    event: session_start
    action: {type: command, command: "echo hi"}
""",
    )
    rules, problems, sources = load_hooks_config(tmp_path)
    assert [r.name for r in rules] == ["a"]
    assert problems == []
    assert sources == [str(tmp_path / ".codeforge" / "hooks.yaml")]


def test_loader_missing_file_ok(tmp_path):
    rules, problems, sources = load_hooks_config(tmp_path)
    assert rules == []
    assert problems == []
    assert sources == []


def test_loader_broken_yaml_discards_level(tmp_path):
    _write(tmp_path / ".codeforge" / "hooks.yaml", "rules: [not valid: [")
    rules, problems, _ = load_hooks_config(tmp_path)
    assert rules == []
    assert any("Skipping hooks config" in p for p in problems)


def test_loader_single_invalid_rule_discarded(tmp_path):
    _write(
        tmp_path / ".codeforge" / "hooks.yaml",
        """rules:
  - name: bad
    event: UnknownEvent
    action: {type: command, command: "x"}
  - name: good
    event: session_start
    action: {type: command, command: "x"}
""",
    )
    rules, problems, _ = load_hooks_config(tmp_path)
    assert [r.name for r in rules] == ["good"]
    assert any('unknown event "UnknownEvent"' in p for p in problems)


def test_loader_async_blocking_rejected(tmp_path):
    _write(
        tmp_path / ".codeforge" / "hooks.yaml",
        """rules:
  - name: bad-async
    event: pre_tool
    async: true
    action: {type: command, command: "x"}
""",
    )
    rules, problems, _ = load_hooks_config(tmp_path)
    assert rules == []
    assert any("async not allowed for blocking events" in p for p in problems)


def test_loader_all_and_any_rejected(tmp_path):
    _write(
        tmp_path / ".codeforge" / "hooks.yaml",
        """rules:
  - name: both
    event: session_start
    if:
      all: [{field: x, op: exact, value: "1"}]
      any: [{field: y, op: exact, value: "2"}]
    action: {type: command, command: "x"}
""",
    )
    rules, problems, _ = load_hooks_config(tmp_path)
    assert rules == []
    assert any('both "all" and "any"' in p for p in problems)


def test_loader_duration_string_parsed(tmp_path):
    _write(
        tmp_path / ".codeforge" / "hooks.yaml",
        """rules:
  - name: slow
    event: session_start
    timeout: 30s
    action: {type: command, command: "x"}
""",
    )
    rules, _, _ = load_hooks_config(tmp_path)
    assert rules[0].timeout == 30.0


def test_loader_invalid_timeout_discarded(tmp_path):
    _write(
        tmp_path / ".codeforge" / "hooks.yaml",
        """rules:
  - name: bad
    event: session_start
    timeout: abc
    action: {type: command, command: "x"}
""",
    )
    rules, problems, _ = load_hooks_config(tmp_path)
    assert rules == []
    assert any("invalid timeout" in p for p in problems)


def test_loader_project_wins_on_name_conflict(tmp_path, monkeypatch):
    _write(
        tmp_path / ".codeforge" / "hooks.yaml",
        """rules:
  - name: dup
    event: session_start
    action: {type: command, command: "echo p"}
""",
    )
    home = tmp_path / "home"
    home.mkdir()
    _write(
        home / ".codeforge" / "hooks.yaml",
        """rules:
  - name: dup
    event: session_start
    action: {type: command, command: "echo u"}
  - name: only-user
    event: session_start
    action: {type: command, command: "echo u2"}
""",
    )
    monkeypatch.setattr("core.hooks.loader.Path.home", lambda: home)
    rules, problems, sources = load_hooks_config(tmp_path)
    assert [r.name for r in rules] == ["dup", "only-user"]
    assert any("name conflict" in p for p in problems)
    assert len(sources) == 2  # 项目 + 用户都列出


# ── Runner: 拦截判定 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_tool_block_on_nonzero_exit(tmp_path):
    import sys

    runner = HookRunner(
        rules=[
            _rule(
                name="b",
                event="pre_tool",
                command=f"{sys.executable} -c \"import sys; print('DENIED'); sys.exit(1)\"",
            )
        ],
        cwd=tmp_path,
    )
    blocked, reason = await runner.check_pre_tool(
        _ctx("pre_tool", tool_name="write_file", input={})
    )
    assert blocked is True
    assert reason == "DENIED"


@pytest.mark.asyncio
async def test_pre_tool_allow_on_exit_zero(tmp_path):
    runner = HookRunner(
        rules=[_rule(name="a", event="pre_tool", command="exit 0")],
        cwd=tmp_path,
    )
    blocked, _ = await runner.check_pre_tool(_ctx("pre_tool", tool_name="write_file"))
    assert blocked is False


@pytest.mark.asyncio
async def test_pre_tool_timeout_fail_open(tmp_path):
    runner = HookRunner(
        rules=[_rule(name="slow", event="pre_tool", command="sleep 2", timeout=0.1)],
        cwd=tmp_path,
    )
    blocked, _ = await runner.check_pre_tool(_ctx("pre_tool", tool_name="write_file"))
    assert blocked is False  # 超时 = hook 失败，fail-open 放行


@pytest.mark.asyncio
async def test_pre_tool_spawn_failure_fail_open(tmp_path, monkeypatch):
    async def _boom(*args, **kwargs):
        raise OSError("no shell")

    monkeypatch.setattr("core.hooks.runner.asyncio.create_subprocess_shell", _boom)
    runner = HookRunner(
        rules=[_rule(name="x", event="pre_tool", command="whatever")],
        cwd=tmp_path,
    )
    blocked, _ = await runner.check_pre_tool(_ctx("pre_tool"))
    assert blocked is False  # 生成子进程失败 → fail-open


@pytest.mark.asyncio
async def test_once_fires_only_once_per_session(tmp_path):
    runner = HookRunner(
        rules=[_rule(name="o", event="turn_start", command="echo x", once=True)],
        cwd=tmp_path,
    )
    await runner.run("turn_start", _ctx("turn_start"))
    assert "o" in runner._once
    await runner.run("turn_start", _ctx("turn_start"))  # 第二次命中跳过
    assert runner._once == {"o"}


@pytest.mark.asyncio
async def test_reset_clears_once(tmp_path):
    runner = HookRunner(
        rules=[_rule(name="o", event="turn_start", command="echo x", once=True)],
        cwd=tmp_path,
    )
    await runner.run("turn_start", _ctx("turn_start"))
    assert "o" in runner._once
    runner.reset()
    assert runner._once == set()
    assert runner.inject_store().snapshot() == []


@pytest.mark.asyncio
async def test_async_hook_runs_in_background(tmp_path):
    logf = tmp_path / "bg.txt"
    runner = HookRunner(
        rules=[
            _rule(
                name="bg",
                event="turn_start",
                command=f'echo bg > "{logf}"',
                async_run=True,
            )
        ],
        cwd=tmp_path,
    )
    await runner.run("turn_start", _ctx("turn_start"))
    for _ in range(100):
        if logf.exists():
            break
        await asyncio.sleep(0.01)
    assert logf.exists()
    assert logf.read_text().strip() == "bg"


# ── Inject ─────────────────────────────────────────────────────────


def test_inject_store_add_snapshot_clear():
    store = InjectionStore()
    store.add("a", "hello")
    store.add("b", "world")
    assert store.snapshot() == ["hello", "world"]
    store.clear()
    assert store.snapshot() == []


@pytest.mark.asyncio
async def test_prompt_action_injects(tmp_path):
    runner = HookRunner(
        rules=[
            _rule(
                name="inj",
                event="turn_start",
                action_type="prompt",
                action_kwargs={"content": "先跑测试"},
            )
        ],
        cwd=tmp_path,
    )
    await runner.run("turn_start", _ctx("turn_start"))
    assert runner.inject_store().snapshot() == ["先跑测试"]


# ── HTTP ───────────────────────────────────────────────────────────


async def _http_capture():
    captured: dict = {}

    async def handler(reader, writer):
        # 单次 read() 在负载下可能只拿到请求头——TCP 会把头和正文分到不同段，
        # 于是正文缺失、断言随机失败。这里按 Content-Length 循环读到完整请求。
        data = await reader.read(65536)
        head, _, body = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
                break
        while len(body) < length:
            chunk = await reader.read(65536)
            if not chunk:
                break
            body += chunk
        captured["data"] = head + b"\r\n\r\n" + body
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, captured


@pytest.mark.asyncio
async def test_http_body_template_renders(tmp_path, monkeypatch):
    # httpx 默认 `trust_env=True`，会读 `HTTP_PROXY`/`HTTPS_PROXY`。本机环境设了
    # 一个本机代理却没设 `NO_PROXY`，于是连 127.0.0.1 的请求也被送去代理，
    # 根本到不了下面的测试服务器（表现为 `captured` 里没有 `data`，很像时序问题）。
    # 这里显式把 loopback 排除掉：本用例要测的是**模板渲染**，不是代理行为。
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    server, port, captured = await _http_capture()
    async with server:
        rule = HookRule(
            name="h",
            event="turn_start",
            action=HookAction(
                type="http",
                url=f"http://127.0.0.1:{port}/x",
                body="event={event}",
            ),
        )
        runner = HookRunner(rules=[rule], cwd=tmp_path)
        await runner.run("turn_start", _ctx("turn_start", session_id="s1"))
        body = captured["data"].split(b"\r\n\r\n", 1)[1]
        assert b"event=turn_start" in body


# ── Subagent 占位 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subagent_stub_logs_and_noop(tmp_path, capsys):
    rule = HookRule(
        name="sub",
        event="turn_start",
        action=HookAction(type="subagent", prompt="do x"),
    )
    runner = HookRunner(rules=[rule], cwd=tmp_path)
    await runner.run("turn_start", _ctx("turn_start"))
    err = capsys.readouterr().err
    assert "[hook subagent] not yet implemented, skipped: sub" in err


# ── Agent 集成 ─────────────────────────────────────────────────────


def _make_agent(responses, runner, registry, cwd, capture=False):
    from core.permissions.modes import PermissionMode

    client = _CaptureClient(responses) if capture else MockLLMClient(responses)
    conv = ConversationManager(system_prompt="You are CodeForge.")
    agent = Agent(
        registry=registry,
        llm_client=client,
        exec_ctx=ExecutionContext(cwd=Path(cwd), session_id="test"),
        conversation=conv,
        hooks=runner,
    )
    agent.set_permission_mode(PermissionMode.BYPASS)  # 测试环境跳过 HITL
    return agent, client, conv


@pytest.mark.asyncio
async def test_agent_prompt_injection_in_system_prompt(tmp_path):
    runner = HookRunner(
        rules=[
            _rule(
                name="inj",
                event="turn_start",
                action_type="prompt",
                action_kwargs={"content": "先跑测试"},
            )
        ],
        cwd=tmp_path,
    )
    # 预注入（模拟上一轮 turn_start 已触发）
    await runner.run("turn_start", _ctx("turn_start", cwd=str(tmp_path)))
    assert "先跑测试" in runner.inject_store().snapshot()[0]

    reg = ToolRegistry()
    from tests.test_agent import WriteTool

    reg.register(WriteTool())
    agent, client, _ = _make_agent(
        [[TextChunk("ok"), CompletionDone()]], runner, reg, tmp_path, capture=True
    )
    async for _ in agent.run("hello"):
        pass
    await asyncio.sleep(0.05)  # 排空后台 emit task
    assert any(
        "## Hook Injections" in sp and "先跑测试" in sp for sp in client.captured
    )


@pytest.mark.asyncio
async def test_agent_pre_tool_intercepts_tool(tmp_path):
    import sys

    runner = HookRunner(
        rules=[
            _rule(
                name="block",
                event="pre_tool",
                command=f"{sys.executable} -c \"import sys; print('DENIED'); sys.exit(1)\"",
            )
        ],
        cwd=tmp_path,
    )
    reg = ToolRegistry()
    from tests.test_agent import WriteTool

    reg.register(WriteTool())
    agent, _, conv = _make_agent(
        [
            [
                ToolUse(id="tu1", name="write_test", input={"path": "a"}),
                CompletionDone(),
            ],
            [TextChunk("blocked"), CompletionDone()],
        ],
        runner,
        reg,
        tmp_path,
    )
    async for _ in agent.run("write a"):
        pass
    await asyncio.sleep(0.05)

    tool_msgs = [
        m.content
        for m in conv.messages
        if m.role == MessageRole.USER
        and m.tool_use_id
        and "Blocked by hook" in str(m.content)
    ]
    assert tool_msgs, "expected a blocked tool result in conversation"
    assert "Error: Blocked by hook: DENIED" in tool_msgs[0]


# ── /hooks 命令 ────────────────────────────────────────────────────


def test_hooks_command_empty_and_listed(capsys):
    from core.commands.builtin_hooks import handle_hooks
    from core.commands.ui import NopUI

    class StubUI(NopUI):
        def __init__(self):
            self.lines = []

        def println(self, msg):
            self.lines.append(msg)

    ui = StubUI()
    asyncio.run(handle_hooks(ui))
    assert ui.lines == ["No hooks loaded."]

    ui2 = StubUI()
    ui2.hook_rules = lambda: [
        HookRule(
            name="a",
            event="pre_tool",
            action=HookAction(type="command", command="x"),
            once=True,
        ),
        HookRule(
            name="b",
            event="session_start",
            action=HookAction(type="prompt", content="z"),
        ),
    ]
    ui2.hook_sources = lambda: ["/p/.codeforge/hooks.yaml"]
    asyncio.run(handle_hooks(ui2))
    assert ui2.lines[0].startswith("  a  pre_tool  command [once]")
    assert ui2.lines[1].startswith("  b  session_start  prompt")
    assert ui2.lines[-1] == "Loaded from: /p/.codeforge/hooks.yaml"
