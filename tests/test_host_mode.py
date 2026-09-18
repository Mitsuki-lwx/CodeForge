"""TUI host 客户端模式测试（任务 16）。

这里测三件容易出错、且不用起真终端就能测的事：

1. **配置闸门**：`features.host.enabled` 读不出来时必须退回"关"——默认路径的行为
   不变是这一条最硬的约束。
2. **连接 vs 内嵌的抉择**：有 host 就只连（**不许**动别人的 host、不许再开一个
   run）；没有 host 才内嵌；握手被拒**不许**内嵌（那会把"token 不对"盖成"已有活跃
   run"，用户会查错方向）。
3. **交互循环**：投一条消息 → 渲染到回合结束 → 退出码与副作用都对。

与 `test_host_client.py` 同一取舍：用真的 `start_host`（真装配、真锁、真 run 记录、
真 TCP），只在需要事件流时把 `bundle.agent` 换成桩 agent。

时序上需要确定的地方一律用**闸门 agent**（跑到一半等一个 Event），而不是 sleep——
sleep 出来的"稳定"是运气，闸门是因果。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from config.loader import load_host_config
from config.model import HostConfig, ProviderConfig
from core.agent.events import AgentError, AgentFinished, TextDelta
from core.host.client import HostClient
from core.host.protocol import load_host_info
from core.host.run_store import RunStatus, RunStore
from core.host.server import start_host
from tui.host_mode import (
    _client_loop,
    _drain_turn,
    _expected_turn_ends,
    _render_frame,
    run_host_mode,
)

MINIMAL_CONFIG = """\
providers:
  - name: "Test"
    protocol: openai
    model: "gpt-4o"
    api_key: "sk-test-not-a-real-key"
"""


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        protocol="openai",
        model="gpt-4o",
        api_key="sk-test-not-a-real-key",
    )


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _console() -> Console:
    """把输出丢进内存，别把测试日志刷满终端。"""
    return Console(file=io.StringIO())


class _StubAgent:
    """假 agent：事件完全可控，不碰网络与真模型。"""

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.script: dict[str, list[Any]] = {}

    async def run(self, text: str):
        self.turns.append(text)
        events = self.script.get(
            text, [TextDelta(text=f"echo:{text}"), AgentFinished(text=f"echo:{text}")]
        )
        for ev in events:
            yield ev

    def resolve_hitl(self, tool_id: str, allowed: bool, choice: str = "") -> None:
        pass


class _GatedAgent:
    """回合开始后等一个闸门，用来把"host 正忙"这个时序做确定。

    不开闸门 = 回合永远跑不完，这正是测"等所有回合"与"中途断连"需要的状态。
    """

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def run(self, text: str):
        self.turns.append(text)
        self.started.set()
        await self.gate.wait()
        yield TextDelta(text=f"echo:{text}")
        yield AgentFinished(text=f"echo:{text}")

    def resolve_hitl(self, tool_id: str, allowed: bool, choice: str = "") -> None:
        pass


class _ScriptedPrompt:
    """按脚本喂输入；用尽后抛 `EOFError`（等价于用户按了 Ctrl-D）。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.prompts = 0

    async def prompt_async(self, *args: Any, **kwargs: Any) -> str:
        self.prompts += 1
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


@contextlib.asynccontextmanager
async def _host(tmp_path: Path, *, agent: Any = None):
    """起一个真 host（模拟"另一个终端里 codeforge host"），测完保证关掉。"""
    server = await start_host(provider=_provider(), workspace=tmp_path)
    if agent is not None:
        server.bundle.agent = agent
    try:
        yield server
    finally:
        server.request_shutdown()
        await server.wait_closed()


# ── 配置闸门：读不出来必须是"关" ────────────────────────────────


def test_host_config_defaults_to_disabled(tmp_path):
    """没有 features.host 时必须是关的——默认路径行为不变靠这一条。"""
    cfg = load_host_config(_write_config(tmp_path, MINIMAL_CONFIG))
    assert isinstance(cfg, HostConfig)
    assert cfg.enabled is False
    assert cfg.unattended_policy == "deny_all"
    assert cfg.port == 0


def test_host_config_reads_enabled(tmp_path):
    cfg = load_host_config(
        _write_config(
            tmp_path,
            MINIMAL_CONFIG
            + "\nfeatures:\n  host:\n    enabled: true\n"
            "    port: 45678\n    unattended_policy: allow_write\n",
        )
    )
    assert cfg.enabled is True
    assert cfg.port == 45678
    assert cfg.unattended_policy == "allow_write"


def test_host_config_broken_yaml_falls_back_to_disabled(tmp_path):
    """坏配置不许抛异常，也不许当成"开着"——按默认档（关）走原路径。"""
    cfg = load_host_config(_write_config(tmp_path, "providers: [ oops\n"))
    assert cfg.enabled is False


def test_host_config_invalid_policy_falls_back(tmp_path):
    """非法档位退回 deny_all（与 `_parse_host_config` 的取舍一致），但仍尊重 enabled。"""
    cfg = load_host_config(
        _write_config(
            tmp_path,
            MINIMAL_CONFIG
            + "\nfeatures:\n  host:\n    enabled: true\n    unattended_policy: 随便放\n",
        )
    )
    assert cfg.enabled is True
    assert cfg.unattended_policy == "deny_all"


# ── 连不上 → 内嵌；连得上 → 只连 ───────────────────────────────


async def test_embeds_host_when_none_running(tmp_path):
    """没有 host 时内嵌一个，退出后 run 落 completed、会合文件清掉。"""
    code = await run_host_mode(
        providers=[_provider()],
        prompt_source=_ScriptedPrompt(["/quit"]),
        workspace=tmp_path,
    )
    assert code == 0

    runs = RunStore(tmp_path).list()
    assert len(runs) == 1
    # 内嵌 host 随本进程退出而收尾：run 不能停在 running，否则下一次启动会被
    # latest_active() 判成"已有活跃 run"而永远起不来
    assert runs[0].status is RunStatus.COMPLETED
    assert load_host_info(tmp_path) is None


async def test_connects_to_running_host_and_leaves_it_alone(tmp_path):
    """已有 host 在跑时**只连不内嵌**：不许再开一个 run，也不许把别人的 host 关掉。"""
    async with _host(tmp_path) as server:
        code = await run_host_mode(
            providers=[_provider()],
            prompt_source=_ScriptedPrompt(["/quit"]),
            workspace=tmp_path,
        )
        assert code == 0

        # 只有外部那一个 run，说明没有内嵌（内嵌会 create 出第二个）
        assert len(RunStore(tmp_path).list()) == 1
        # 别人的 host 还在跑：客户端退出不该动它——"客户端断开不影响会话"这条
        # 承诺的反面就是"客户端也不该顺手把它关了"
        assert server.listening
        # 从库里读状态：`server.run` 是 `create()` 那一刻的快照，`start()` 把它推进到
        # running 是在之后发生的，读快照只会看到 queued
        assert RunStore(tmp_path).get(server.run.id).status is RunStatus.RUNNING
        assert load_host_info(tmp_path) is not None


async def test_handshake_rejected_does_not_embed(tmp_path):
    """握手被拒时**不回落内嵌**：host 明明在跑，只是凭据不对。

    回落会撞上 HostAlreadyRunningError，把"token 不对"这个真因盖成"已有活跃 run"，
    用户会去查完全错误的方向。
    """
    async with _host(tmp_path) as server:
        # 换掉磁盘上的 token，服务端内存里的那份没变 → 握手必被拒
        (tmp_path / ".codeforge" / "host.token").write_text(
            "wrong-token", encoding="ascii"
        )

        with pytest.raises(SystemExit) as exc:
            await run_host_mode(
                providers=[_provider()],
                prompt_source=_ScriptedPrompt(["/quit"]),
                workspace=tmp_path,
            )
        assert "握手" in str(exc.value)
        # 没有内嵌：仍然只有外部那一个 run，且它还活着
        assert len(RunStore(tmp_path).list()) == 1
        assert server.listening


async def test_no_provider_reports_clearly(tmp_path):
    """没有 provider 时给出可执行的错误，而不是去起一个注定失败的 host。"""
    with pytest.raises(SystemExit) as exc:
        await run_host_mode(
            providers=[],
            prompt_source=_ScriptedPrompt(["/quit"]),
            workspace=tmp_path,
        )
    assert "provider" in str(exc.value)


# ── 回合计数（纯函数：把竞态与多客户端假设钉住）────────────────


@pytest.mark.parametrize(
    ("ack", "expected"),
    [
        ({"accepted": True, "queued": 1, "busy": False}, 1),
        ({"accepted": True, "queued": 0, "busy": False}, 1),  # 竞态：worker 抢先取走
        ({"accepted": True, "queued": 1, "busy": True}, 2),
        ({"accepted": True, "queued": 3, "busy": True}, 4),
        ({}, 1),  # 字段缺失也不许算成 0
    ],
)
def test_expected_turn_ends(ack, expected):
    assert _expected_turn_ends(ack) == expected


# ── 帧渲染映射 ──────────────────────────────────────────────────


class _RecordingRenderer:
    """只记调用，不碰终端。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.has_started = False

    def on_text(self, text: str) -> None:
        self.has_started = True
        self.calls.append(("text", text))

    def on_thinking(self, text: str) -> None:
        self.has_started = True
        self.calls.append(("thinking", text))

    def on_tool_start(self, name: str, tool_input: dict) -> None:
        self.has_started = True
        self.calls.append(("tool_start", name, tool_input))

    def on_tool_finish(
        self,
        name: str,
        tool_input: dict,
        success: bool,
        preview: str,
        duration_ms: int,
    ) -> None:
        self.calls.append(
            ("tool_finish", name, tool_input, success, preview, duration_ms)
        )


def test_render_frame_maps_streaming_events():
    """帧里的 `data` 是 dataclass 按字段名展开的，所以能直接喂给流式渲染器。"""
    renderer = _RecordingRenderer()
    console = _console()
    assert _render_frame(renderer, console, "TextDelta", {"text": "hi"}) is False
    assert _render_frame(renderer, console, "ThinkingDelta", {"text": "hmm"}) is False
    assert (
        _render_frame(
            renderer, console, "ToolCallStarted", {"name": "bash", "input": {"cmd": "ls"}}
        )
        is False
    )
    assert (
        _render_frame(
            renderer,
            console,
            "ToolCallFinished",
            {
                "name": "bash",
                "input": {"cmd": "ls"},
                "success": True,
                "result_preview": "ok",
                "duration_ms": 12,
            },
        )
        is False
    )
    assert renderer.calls == [
        ("text", "hi"),
        ("thinking", "hmm"),
        ("tool_start", "bash", {"cmd": "ls"}),
        ("tool_finish", "bash", {"cmd": "ls"}, True, "ok", 12),
    ]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("AgentFinished", {"iterations": 2, "elapsed_s": 1.5}),
        ("AgentError", {"message": "boom", "code": "stream_error"}),
        ("HostError", {"message": "回合炸了"}),
    ],
)
def test_render_frame_turn_end_events(name, data):
    """三种结束帧都要被认出来——漏掉一种就会一直等下一条，看起来像卡死。"""
    assert _render_frame(_RecordingRenderer(), _console(), name, data) is True


def test_render_frame_ignores_unknown_events():
    """不认识的事件（IterationUpdate 等）不渲染也不结束回合。"""
    assert _render_frame(_RecordingRenderer(), _console(), "IterationUpdate", {}) is False


# ── 交互循环 ────────────────────────────────────────────────────


async def test_client_loop_sends_message_and_stops_at_turn_end(tmp_path):
    """一条输入 → 投给 host → 渲染到回合结束，然后 /quit 退出。"""
    agent = _StubAgent()
    async with _host(tmp_path, agent=agent):
        async with await HostClient.connect(tmp_path) as client:
            code = await _client_loop(
                _console(), client, prompt_source=_ScriptedPrompt(["hello", "/quit"])
            )
        assert code == 0
        assert agent.turns == ["hello"]


async def test_client_loop_survives_agent_error(tmp_path):
    """回合以 AgentError 收尾时不能卡住——它是结束帧，之后还能继续用。"""
    agent = _StubAgent()
    agent.script["bad"] = [AgentError(message="炸了", code="stream_error")]
    async with _host(tmp_path, agent=agent):
        async with await HostClient.connect(tmp_path) as client:
            code = await _client_loop(
                _console(), client, prompt_source=_ScriptedPrompt(["bad", "/quit"])
            )
        assert code == 0
        assert agent.turns == ["bad"]


async def test_client_loop_unsupported_slash_command_does_not_reach_agent(tmp_path):
    """不支持的斜杠命令必须明确回绝，且**不许**当成用户输入发给 agent。"""
    agent = _StubAgent()
    async with _host(tmp_path, agent=agent):
        async with await HostClient.connect(tmp_path) as client:
            code = await _client_loop(
                _console(), client, prompt_source=_ScriptedPrompt(["/resume", "/quit"])
            )
        assert code == 0
        assert agent.turns == []


async def test_client_loop_eof_exits_cleanly(tmp_path):
    """输入流结束（Ctrl-D）等同退出，不是异常。"""
    async with _host(tmp_path, agent=_StubAgent()):
        async with await HostClient.connect(tmp_path) as client:
            code = await _client_loop(
                _console(), client, prompt_source=_ScriptedPrompt([])
            )
        assert code == 0


async def test_drain_turn_waits_for_every_expected_turn(tmp_path):
    """`expected>1` 时必须把前面排队的回合也渲染完再收工。

    这是"连上时 host 正忙"那一类的核心：只看第一个结束帧就返回，会把我们的回复丢在
    后面没人渲染。
    """
    agent = _GatedAgent()
    async with _host(tmp_path, agent=agent):
        async with await HostClient.connect(tmp_path) as client:
            await client.send_message("a")
            # 等 worker 真的开始跑 "a"，这样下一条必然排队 → busy=True, queued=1
            await asyncio.wait_for(agent.started.wait(), 5)
            ack = await client.send_message("b")
            assert ack["busy"] is True
            assert _expected_turn_ends(ack) == 2

            agent.gate.set()
            assert await _drain_turn(_console(), client, 2) is True
        assert agent.turns == ["a", "b"]


async def test_drain_turn_reports_disconnect(tmp_path):
    """host 中途消失时 `_drain_turn` 返回 False，而不是永远等下去。

    这是客户端最重要的一条"不许挂死"：回合可能跑几分钟，如果 host 在这个窗口里被
    `kill -9`，客户端必须察觉并退出，而不是永远等一个不会来的结束帧。
    """
    agent = _GatedAgent()  # 闸门一直不开 → 永远不会有结束帧
    server = await start_host(provider=_provider(), workspace=tmp_path)
    server.bundle.agent = agent
    client = await HostClient.connect(tmp_path)
    try:
        await client.send_message("never-answered")
        await asyncio.wait_for(agent.started.wait(), 5)

        server.request_shutdown()
        await server.wait_closed()

        # 客户端读循环察觉 EOF 是异步的，不能假定 wait_closed() 返回时它已经跑过；
        # 不设上限的话，这里的"没察觉"会表现成永久挂死而不是断言失败。
        await asyncio.wait_for(_wait_until_closed(client), 5)

        assert await _drain_turn(_console(), client, 1) is False
    finally:
        client.close()


async def _wait_until_closed(client: HostClient) -> None:
    """等客户端读循环结束（有上限由调用方给）。"""
    while not client.closed:
        await asyncio.sleep(0.01)
