"""host 客户端测试。

与 `test_host_protocol.py` 的分工：那边测**服务端**（用自己手搓的测试客户端，顺带
验证协议本身）；这边测**生产客户端**——它能不能按协议把服务端用起来，以及"连不上"
时给出的原因是不是可执行的。

这里刻意用**真的 `start_host`**（真装配、真锁、真 run 记录、真 TCP），只在需要事件流
时把 `bundle.agent` 换成桩 agent。理由：客户端与服务的接缝是"协议 + 会合文件 + 进程
存活判断"，三者里任何一个用假的，测出来的就只是假实现之间的默契。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from config.model import ProviderConfig
from core.agent.events import AgentFinished, TextDelta
from core.host.client import (
    HostClient,
    HostError,
    HostNotRunningError,
)
from core.host.protocol import HostInfo, issue_token, load_host_info, write_host_info
from core.host.run_store import RunStore
from core.host.server import start_host

SESSION_ID = "20260916-160000-cccc"


class _StubAgent:
    """假 agent：不碰网络与真模型，事件完全可控。"""

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


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        protocol="openai",
        model="gpt-4o",
        api_key="sk-test-not-a-real-key",
    )


@contextlib.asynccontextmanager
async def _host(tmp_path: Path, *, stub_agent: bool = False):
    """起一个真 host，测完保证关掉。"""
    server = await start_host(provider=_provider(), workspace=tmp_path)
    if stub_agent:
        server.bundle.agent = _StubAgent()
    try:
        yield server
    finally:
        server.request_shutdown()
        await server.wait_closed()


def _free_port() -> int:
    """要一个**当前没人监听**的端口号。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ── 连不上时的原因必须可执行 ────────────────────────────────────


async def test_connect_without_host_info_says_start_host(tmp_path):
    """没有 host 时消息里要有"先 codeforge host"——只说"连接失败"等于没说。"""
    with pytest.raises(HostNotRunningError) as exc:
        await HostClient.connect(tmp_path)
    assert "codeforge host" in str(exc.value)
    assert "host.json" in exc.value.detail


async def test_connect_with_dead_pid_reports_exit(tmp_path):
    """崩溃的 host 会留下过期 host.json。按 pid 判活，报"进程已退出"而不是
    "端口连不上"——后者会让人去查防火墙。"""
    dead_pid = os.getpid() + 100000
    write_host_info(tmp_path, HostInfo(port=_free_port(), pid=dead_pid, run_id="run-x"))
    with pytest.raises(HostNotRunningError) as exc:
        await HostClient.connect(tmp_path)
    assert str(dead_pid) in exc.value.detail
    assert "已退出" in exc.value.detail


async def test_connect_without_token_reports_token(tmp_path):
    write_host_info(
        tmp_path, HostInfo(port=_free_port(), pid=os.getpid(), run_id="run-x")
    )
    with pytest.raises(HostNotRunningError) as exc:
        await HostClient.connect(tmp_path)
    assert "host.token" in exc.value.detail


async def test_connect_to_unreachable_port_reports_address(tmp_path):
    """pid 活着、token 在，但端口没人听——报出具体地址，便于用户核对。"""
    port = _free_port()
    write_host_info(tmp_path, HostInfo(port=port, pid=os.getpid(), run_id="run-x"))
    issue_token(tmp_path)
    with pytest.raises(HostNotRunningError) as exc:
        await HostClient.connect(tmp_path)
    assert f"127.0.0.1:{port}" in exc.value.detail


async def test_connect_with_wrong_token_raises_host_error(tmp_path):
    """token 不对是"连上了但被拒"，不是"没有 host"——异常类型必须区分开。"""
    async with _host(tmp_path):
        issue_token(tmp_path)  # 覆盖成新 token，与 host 手里的那份不一致
        with pytest.raises(HostError) as exc:
            await HostClient.connect(tmp_path)
        assert "token" in str(exc.value)
        assert exc.value.cmd == "hello"


async def test_connect_timeout_is_bounded(tmp_path):
    """host.json 指向一个**接受连接但从不回应**的端口：不能无限等。"""

    async def silent(reader, writer):
        # 只读到对端关闭，一个字都不写。
        await reader.read()
        # 这个 close() 是必须的，不是礼貌：`asyncio.start_server` 的 done-callback
        # 只在 handler 被取消或抛异常时才关 transport，**正常返回时什么都不做**
        # （见 `StreamReaderProtocol.connection_made`）。不关的话 handler 已经退出、
        # 连接却还开着，`server.wait_closed()` 永远等不到 `_active_count` 归零。
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    write_host_info(tmp_path, HostInfo(port=port, pid=os.getpid(), run_id="run-x"))
    issue_token(tmp_path)
    try:
        with pytest.raises(HostError) as exc:
            await HostClient.connect(tmp_path, timeout=0.3)
        assert "超时" in str(exc.value)
    finally:
        server.close()
        await server.wait_closed()


# ── 命令 ────────────────────────────────────────────────────────


async def test_get_run_and_list_runs(tmp_path):
    async with _host(tmp_path) as server:
        client = await HostClient.connect(tmp_path)
        try:
            assert client.run_id == server.run.id
            run = await client.get_run()
            assert run["id"] == server.run.id
            assert run["is_active"] is True
            assert run["status"] == "running"

            listing = await client.list_runs()
            assert listing["active_run_id"] == server.run.id
            assert [r["id"] for r in listing["runs"]] == [server.run.id]
        finally:
            client.close()


async def test_get_run_unknown_raises_host_error(tmp_path):
    async with _host(tmp_path):
        client = await HostClient.connect(tmp_path)
        try:
            with pytest.raises(HostError) as exc:
                await client.get_run("run-nope")
            assert "run-nope" in str(exc.value)
        finally:
            client.close()


async def test_read_conversation(tmp_path):
    async with _host(tmp_path):
        client = await HostClient.connect(tmp_path)
        try:
            data = await client.read_conversation()
            assert data["run_id"] == client.run_id
            assert data["messages"] == []
        finally:
            client.close()


async def test_send_message_rejects_empty(tmp_path):
    async with _host(tmp_path):
        client = await HostClient.connect(tmp_path)
        try:
            with pytest.raises(HostError):
                await client.send_message("   ")
        finally:
            client.close()


# ── 事件流 ──────────────────────────────────────────────────────


async def test_send_message_streams_events(tmp_path):
    async with _host(tmp_path, stub_agent=True) as server:
        client = await HostClient.connect(tmp_path)
        try:
            accepted = await client.send_message("hi")
            assert accepted["accepted"] is True

            seen: list[str] = []
            async for frame in client.events():
                seen.append(frame["event"])
                if frame["event"] == "AgentFinished":
                    break
            assert seen == ["TextDelta", "AgentFinished"]
            assert server.bundle.agent.turns == ["hi"]
        finally:
            client.close()


async def test_next_event_timeout(tmp_path):
    async with _host(tmp_path, stub_agent=True):
        client = await HostClient.connect(tmp_path)
        try:
            with pytest.raises(TimeoutError):
                await client.next_event(timeout=0.1)
        finally:
            client.close()


async def test_events_iterator_ends_when_host_goes_away(tmp_path):
    """host 关掉后事件迭代器必须**结束**，不能永远挂在那里等一个不会来的事件。"""
    server = await start_host(provider=_provider(), workspace=tmp_path)
    client = await HostClient.connect(tmp_path)
    try:
        collected: list[str] = []

        async def consume() -> None:
            async for _frame in client.events():
                collected.append("event")

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        server.request_shutdown()
        await server.wait_closed()
        await asyncio.wait_for(task, 5.0)  # 迭代器已返回

        assert client.closed
        assert await client.next_event() is None
    finally:
        client.close()


async def test_event_termination_is_sticky(tmp_path):
    """结束判定必须可重复取用，而不是"队列里那条一次性哨兵"。

    用哨兵做终止信号的经典错法：`events()` 循环把它消费掉之后，再调一次
    `next_event()`（不带超时）就会在一个空队列上永远等下去。这条测试用
    `wait_for` 把"永远等"变成失败。
    """
    server = await start_host(provider=_provider(), workspace=tmp_path)
    client = await HostClient.connect(tmp_path)
    try:
        server.request_shutdown()
        await server.wait_closed()
        await asyncio.sleep(0.05)

        async for _frame in client.events():  # 先把哨兵消费掉
            pass
        assert await asyncio.wait_for(client.next_event(), 2.0) is None
        assert await asyncio.wait_for(client.next_event(), 2.0) is None
    finally:
        client.close()


async def test_next_event_without_timeout_after_close_returns_none(tmp_path):
    """连接已经关了的情况下，不带超时的 `next_event()` 也必须立刻返回。"""
    server = await start_host(provider=_provider(), workspace=tmp_path)
    client = await HostClient.connect(tmp_path)
    server.request_shutdown()
    await server.wait_closed()
    client.close()
    assert await asyncio.wait_for(client.next_event(), 2.0) is None


async def test_calls_after_host_gone_raise_connection_error(tmp_path):
    """连接断了之后发命令要立刻失败，而不是等满 30 秒超时。"""
    server = await start_host(provider=_provider(), workspace=tmp_path)
    client = await HostClient.connect(tmp_path)
    try:
        server.request_shutdown()
        await server.wait_closed()
        await asyncio.sleep(0.05)  # 让读循环看到 EOF
        with pytest.raises(ConnectionError):
            await client.get_run()
    finally:
        client.close()


# ── 收尾 ────────────────────────────────────────────────────────


async def test_close_is_idempotent(tmp_path):
    async with _host(tmp_path):
        client = await HostClient.connect(tmp_path)
        client.close()
        client.close()
        assert client.closed


async def test_async_context_manager(tmp_path):
    async with _host(tmp_path):
        async with await HostClient.connect(tmp_path) as client:
            assert (await client.get_run())["id"] == client.run_id
        assert client.closed


async def test_handshake_failure_does_not_leak_connection(tmp_path):
    """握手失败必须把连接与读循环收掉，否则每失败一次漏一条连接 + 一个任务。"""
    async with _host(tmp_path):
        issue_token(tmp_path)  # 让 token 失效
        before = len(asyncio.all_tasks())
        for _ in range(5):
            with pytest.raises(HostError):
                await HostClient.connect(tmp_path)
        await asyncio.sleep(0.05)
        assert len(asyncio.all_tasks()) <= before


# ── 与 run 记录的衔接 ───────────────────────────────────────────


async def test_client_sees_stale_run_reclaimed(tmp_path):
    """客户端看到的状态是 host 启动时改判过的，不是库里的谎话。"""
    store = RunStore(tmp_path)
    zombie = store.create(SESSION_ID, pid=None)

    async with _host(tmp_path):
        client = await HostClient.connect(tmp_path)
        try:
            assert (await client.get_run(zombie.id))["status"] == "interrupted"
        finally:
            client.close()


async def test_workspace_and_info_are_exposed(tmp_path):
    async with _host(tmp_path) as server:
        client = await HostClient.connect(tmp_path)
        try:
            assert client.workspace == tmp_path.resolve()
            assert client.info.port == server.port
            assert load_host_info(tmp_path) == client.info
        finally:
            client.close()


async def test_conversation_survives_in_a_fresh_manager(tmp_path):
    """客户端拿到的对话来自 host 的内存，而不是本进程另开的会话。"""
    async with _host(tmp_path) as server:
        server.bundle.conversation.add_user_message("hello from host")
        client = await HostClient.connect(tmp_path)
        try:
            data = await client.read_conversation()
            assert [m["content"] for m in data["messages"]] == ["hello from host"]
        finally:
            client.close()


async def test_connect_uses_fresh_token_after_restart(tmp_path):
    """host 重启换新 token；客户端每次都重读文件，不会拿着旧凭据反复失败。"""
    async with _host(tmp_path):
        first = await HostClient.connect(tmp_path)
        first.close()

    async with _host(tmp_path):
        second = await HostClient.connect(tmp_path)
        try:
            assert (await second.get_run())["status"] == "running"
        finally:
            second.close()


def test_client_does_not_import_server_stack():
    """客户端不该为了连一次 host 就把 agent 装配栈拉起来。

    这条是**结构约束**，不是行为测试：一旦有人在 `client.py` 里 `import server`
    （或间接经它 import `bootstrap`），连接一个 host 的启动成本就从"几十毫秒"变成
    "装配全部工具/provider"。用子进程单独 import `core.host.client` 来验证。
    """
    code = (
        "import sys, core.host.client;"
        "print('core.agent.bootstrap' in sys.modules);"
        "print('core.host.server' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        check=True,
    )
    assert proc.stdout.split() == ["False", "False"], proc.stdout
