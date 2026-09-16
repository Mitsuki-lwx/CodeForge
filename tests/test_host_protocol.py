"""host 控制通道测试。

分三层：
1. 协议编解码与 token（纯函数，最便宜也最容易出错的地方）。
2. `HostServer` 的行为——用**桩 bundle**（假 agent）驱动，事件、回合、HITL 处置、
   关闭语义全部确定可控，不碰网络与真模型。
3. 真接线：`start_host()` 装配真会话，验证锁、run 记录、单活跃 run 限制。

第 2 层刻意不用真 agent：`send_message` 会真的调模型，测试里那是网络依赖 + 花钱。

**测试客户端要点**：应答与事件**可以交错**（服务端先推事件后回 ack 是允许的），
所以客户端必须靠 `id` 关联应答、把 `event` 帧分流到事件队列——这和任务 11 的正式
客户端是同一套逻辑。测试里用 `request()` 拿应答、`recv()` 拿事件。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent.events import (
    AgentFinished,
    HITLRequired,
    TextDelta,
    ToolCallFinished,
)
from core.host import protocol as protocol_mod
from core.host.lifecycle import RunLifecycle
from core.host.protocol import (
    HOST_INFO_FILENAME,
    HOST_TOKEN_FILENAME,
    HostInfo,
    ProtocolError,
    clear_host_info,
    decode_frame,
    encode_frame,
    event_frame,
    host_info_path,
    issue_token,
    load_host_info,
    load_token,
    to_jsonable,
    token_matches,
    token_path,
    write_host_info,
)
from core.host.run_store import RunStatus, RunStore
from core.host.server import (
    CLIENT_QUEUE_SIZE,
    HostAlreadyRunningError,
    HostServer,
    start_host,
)

SESSION_ID = "20260916-160000-aaaa"


# ── 桩件 ────────────────────────────────────────────────────────


class _StubAgent:
    """假 agent：按输入回放一段固定事件，并记录 HITL 处置。"""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, bool, str]] = []
        self.turns: list[str] = []
        self.script: dict[str, list[Any]] = {}

    async def run(self, text: str):
        self.turns.append(text)
        events = self.script.get(
            text, [TextDelta(text=f"echo:{text}"), AgentFinished(text=f"echo:{text}")]
        )
        for ev in events:
            yield ev

    def resolve_hitl(self, tool_use_id: str, allowed: bool, choice: str = "") -> None:
        self.resolved.append((tool_use_id, allowed, choice))


@dataclass
class _StubBundle:
    workspace: Path
    agent: Any = field(default_factory=_StubAgent)
    conversation: ConversationManager = field(default_factory=ConversationManager)
    session_id: str = SESSION_ID
    closed: bool = False

    @property
    def runtime(self) -> Any:
        return SimpleNamespace(session=SimpleNamespace(session_id=self.session_id))

    def close(self) -> None:
        self.closed = True


class _TestClient:
    """说协议的最小客户端：后台读帧，按 `id` 关联应答、按 `event` 分流事件。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._counter = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue[dict] = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(cls, port: int) -> _TestClient:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return cls(reader, writer)

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                frame = decode_frame(line)
                fut = self._pending.get(str(frame.get("id")))
                if fut is not None and not fut.done():
                    fut.set_result(frame)
                else:
                    # 事件帧，或关联不上的杂帧——都进事件流让测试能看见
                    self._events.put_nowait(frame)
        except (ConnectionError, ProtocolError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("连接已关闭"))
            self._pending.clear()
            self._events.put_nowait({"event": "_closed"})

    async def _call(
        self,
        cmd: str,
        token: str | None = None,
        protocol: int | None = protocol_mod.PROTOCOL_VERSION,
        **args,
    ) -> dict:
        self._counter += 1
        req_id = str(self._counter)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload: dict[str, Any] = {"id": req_id, "cmd": cmd, "args": args}
        if token is not None:
            payload["token"] = token
        if protocol is not None:
            payload["protocol"] = protocol
        self._writer.write(encode_frame(payload))
        await self._writer.drain()
        return await asyncio.wait_for(fut, 5.0)

    async def request(self, cmd: str, **args) -> dict:
        return await self._call(cmd, **args)

    async def hello(
        self, token: str | None, protocol: int | None = protocol_mod.PROTOCOL_VERSION
    ) -> dict:
        return await self._call("hello", token=token, protocol=protocol)

    async def send_raw_bytes(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def recv(self, timeout: float = 5.0) -> dict:
        frame = await asyncio.wait_for(self._events.get(), timeout)
        assert frame.get("event") != "_closed", "连接已关闭"
        return frame

    def drain_events(self) -> list[dict]:
        """取出当前已收到的事件帧（含 `_closed` 哨兵），不等待。"""
        out: list[dict] = []
        while not self._events.empty():
            out.append(self._events.get_nowait())
        return out

    async def wait_closed(self, timeout: float = 5.0) -> None:
        """等服务端断开连接。"""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._reader_task), timeout)

    def close(self) -> None:
        self._reader_task.cancel()
        self._writer.close()


async def _connected(server: HostServer) -> _TestClient:
    client = await _TestClient.connect(server.port)
    resp = await client.hello(load_token(server.workspace))
    assert resp["ok"], resp
    return client


def _make_server(tmp_path: Path, *, store: RunStore | None = None) -> HostServer:
    run_store = store or RunStore(tmp_path)
    lifecycle = RunLifecycle(run_store)
    run = run_store.create(SESSION_ID)
    return HostServer(
        _StubBundle(workspace=tmp_path),
        store=run_store,
        run=run,
        lifecycle=lifecycle,
    )


# ── 协议编解码 ──────────────────────────────────────────────────


def test_frame_round_trip():
    payload = {"id": "1", "cmd": "list_runs", "args": {"limit": 5}}
    assert decode_frame(encode_frame(payload)) == payload


def test_frame_never_contains_raw_newline():
    """换行分隔能成立的前提：一个对象永远只占一行。"""
    frame = encode_frame({"text": "line1\nline2"})
    assert frame.count(b"\n") == 1
    assert frame.endswith(b"\n")


def test_decode_rejects_non_json():
    with pytest.raises(ProtocolError):
        decode_frame(b"not json\n")


def test_decode_rejects_non_object():
    with pytest.raises(ProtocolError):
        decode_frame(b"[1, 2, 3]\n")


def test_decode_accepts_str_and_bytes():
    assert decode_frame(b'{"a": 1}\n') == {"a": 1}
    assert decode_frame('{"a": 1}') == {"a": 1}


def test_to_jsonable_handles_dataclass_and_enum():
    assert to_jsonable(TextDelta(text="hi")) == {"text": "hi"}
    assert to_jsonable(RunStatus.RUNNING) == "running"
    assert to_jsonable({"a": (1, 2)}) == {"a": [1, 2]}
    assert to_jsonable(None) is None


def test_to_jsonable_stringifies_unknown_objects():
    """未知对象降级成字符串，绝不让推送在 json.dumps 上炸掉整条连接。"""
    assert to_jsonable(RuntimeError("boom")) == "boom"


def test_event_frame_uses_class_name():
    assert event_frame(TextDelta(text="x")) == {
        "event": "TextDelta",
        "data": {"text": "x"},
    }


def test_tool_call_finished_is_json_serializable():
    ev = ToolCallFinished(
        tool_use_id="t1",
        name="bash",
        input={"command": "ls"},
        success=True,
        result_preview="ok",
        duration_ms=3,
    )
    assert json.loads(json.dumps(event_frame(ev)))["event"] == "ToolCallFinished"


# ── token ───────────────────────────────────────────────────────


def test_issue_and_load_token_round_trip(tmp_path):
    token = issue_token(tmp_path)
    assert len(token) >= 32
    assert load_token(tmp_path) == token
    assert token_path(tmp_path) == tmp_path / ".codeforge" / HOST_TOKEN_FILENAME


def test_issue_token_rotates(tmp_path):
    """每次启动换新 token——上一轮 host 的凭据不该继续有效。"""
    assert issue_token(tmp_path) != issue_token(tmp_path)


def test_load_token_missing_returns_none(tmp_path):
    assert load_token(tmp_path) is None


def test_token_file_mode_is_0600_on_posix(tmp_path):
    issue_token(tmp_path)
    if os.name == "nt":
        # Windows 的 os.chmod 只切只读位，设不了 ACL——见 protocol 模块 docstring
        pytest.skip("Windows 无法用 chmod 收紧权限，已在 docstring 中说明降级")
    assert stat.S_IMODE(token_path(tmp_path).stat().st_mode) == 0o600


def test_token_matches():
    assert token_matches("abc", "abc")
    assert not token_matches("abc", "abd")
    assert not token_matches("abc", None)
    assert not token_matches("abc", 123)
    assert not token_matches(None, "abc")
    assert not token_matches("", "")


# ── 会合：host 在哪 ─────────────────────────────────────────────


def test_host_info_round_trip(tmp_path):
    info = HostInfo(port=54321, pid=4242, run_id="run-20260916-160000-aaaa")
    write_host_info(tmp_path, info)
    assert load_host_info(tmp_path) == info
    assert host_info_path(tmp_path) == tmp_path / ".codeforge" / HOST_INFO_FILENAME


def test_load_host_info_missing_returns_none(tmp_path):
    assert load_host_info(tmp_path) is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[1, 2, 3]",
        '{"port": "x", "pid": 1, "run_id": "r"}',
        '{"port": 1, "run_id": "r"}',
        '{"port": 1, "pid": 2}',
        "{}",
    ],
)
def test_load_host_info_broken_returns_none(tmp_path, content):
    """坏文件与没文件归一成 `None`——客户端对两者的处置是同一件事。"""
    path = host_info_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    assert load_host_info(tmp_path) is None


def test_clear_host_info_only_removes_own(tmp_path):
    """只删自己那份：否则旧 host 收尾会把刚启动的新 host 的会合信息删掉。"""
    write_host_info(tmp_path, HostInfo(port=1, pid=os.getpid() + 1, run_id="other"))
    assert clear_host_info(tmp_path, pid=os.getpid()) is False
    assert load_host_info(tmp_path) is not None

    write_host_info(tmp_path, HostInfo(port=1, pid=os.getpid(), run_id="mine"))
    assert clear_host_info(tmp_path, pid=os.getpid()) is True
    assert load_host_info(tmp_path) is None
    # 幂等：再删一次不报错，返回 False
    assert clear_host_info(tmp_path, pid=os.getpid()) is False


# ── 握手与鉴权 ──────────────────────────────────────────────────


async def test_handshake_returns_run_info(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _TestClient.connect(server.port)
        resp = await client.hello(load_token(tmp_path))
        assert resp["ok"]
        assert resp["data"]["run"]["id"] == server.run.id
        assert resp["data"]["protocol"] == protocol_mod.PROTOCOL_VERSION
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_handshake_rejects_missing_token(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _TestClient.connect(server.port)
        resp = await client.hello(None)
        assert not resp["ok"]
        assert "token" in resp["error"]
        await client.wait_closed()
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_handshake_rejects_wrong_token(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _TestClient.connect(server.port)
        assert not (await client.hello("definitely-not-the-token"))["ok"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_handshake_rejects_first_frame_not_hello(tmp_path):
    """首帧不是 hello 也要拒——不能靠"先发命令看看"来探数据。"""
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _TestClient.connect(server.port)
        assert not (await client.request("list_runs"))["ok"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_handshake_rejects_protocol_mismatch(tmp_path):
    """版本不匹配当场说清楚，别让对方连上之后栽在一堆 KeyError 上。"""
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _TestClient.connect(server.port)
        resp = await client.hello(load_token(tmp_path), protocol=99)
        assert not resp["ok"]
        assert "协议版本" in resp["error"]
        assert "99" in resp["error"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_rejected_client_receives_no_run_data(tmp_path):
    """被拒的连接不该拿到任何 run 数据，且随即被断开。

    拒绝应答带 `id`，会被客户端按应答关联走；事件流里剩下的只该有"连接已关闭"哨兵。
    """
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _TestClient.connect(server.port)
        assert not (await client.hello("wrong"))["ok"]
        await client.wait_closed()
        assert client.drain_events() == [{"event": "_closed"}]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_invalid_frame_does_not_kill_host(tmp_path):
    """一条坏帧只该毁掉这一条应答，不该掀掉 host。"""
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _connected(server)
        await client.send_raw_bytes(b"{not json}\n")
        assert not (await client.recv())["ok"]
        assert (await client.request("list_runs"))["ok"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


# ── 命令 ────────────────────────────────────────────────────────


async def test_list_runs_includes_active(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _connected(server)
        data = (await client.request("list_runs"))["data"]
        assert data["active_run_id"] == server.run.id
        assert [r["id"] for r in data["runs"]] == [server.run.id]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_get_run_defaults_to_active(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _connected(server)
        data = (await client.request("get_run"))["data"]
        assert data["id"] == server.run.id
        assert data["is_active"] is True
        # start() 已经把 run 从 queued 推进到 running——一个在监听、在收活的 host
        # 对外报 queued 就是撒谎
        assert data["status"] == "running"
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_get_run_unknown_is_error_not_crash(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _connected(server)
        resp = await client.request("get_run", run_id="run-nope")
        assert not resp["ok"]
        assert "run-nope" in resp["error"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_unknown_command_is_error(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _connected(server)
        assert not (await client.request("no_such_command"))["ok"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_read_conversation_active_uses_memory(tmp_path):
    server = _make_server(tmp_path)
    server.bundle.conversation.add_user_message("hello")
    await server.start()
    try:
        client = await _connected(server)
        data = (await client.request("read_conversation"))["data"]
        assert data["total"] == 1
        assert data["messages"][0]["content"] == "hello"
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_read_conversation_respects_limit(tmp_path):
    server = _make_server(tmp_path)
    for i in range(5):
        server.bundle.conversation.add_user_message(f"m{i}")
    await server.start()
    try:
        client = await _connected(server)
        data = (await client.request("read_conversation", limit=2))["data"]
        assert data["total"] == 5
        assert [m["content"] for m in data["messages"]] == ["m3", "m4"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_read_conversation_non_active_reads_disk(tmp_path):
    """非活跃 run 没有内存对话，只能读磁盘 JSONL。"""
    store = RunStore(tmp_path)
    other = store.create("20260101-000000-bbbb")
    server = _make_server(tmp_path, store=store)
    await server.start()
    try:
        session_dir = tmp_path / ".codeforge" / "sessions" / other.session_id
        session_dir.mkdir(parents=True)
        (session_dir / "conversation.jsonl").write_text(
            json.dumps({"role": "user", "content": "from disk"}) + "\n",
            encoding="utf-8",
        )
        client = await _connected(server)
        data = (await client.request("read_conversation", run_id=other.id))["data"]
        assert [m["content"] for m in data["messages"]] == ["from disk"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_send_message_rejects_empty(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _connected(server)
        assert not (await client.request("send_message", text="   "))["ok"]
        assert not (await client.request("send_message"))["ok"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


# ── 回合循环与事件推送 ──────────────────────────────────────────


async def test_send_message_runs_turn_and_streams_events(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        client = await _connected(server)
        assert (await client.request("send_message", text="hi"))["data"]["accepted"]
        assert (await client.recv())["event"] == "TextDelta"
        assert (await client.recv())["event"] == "AgentFinished"
        assert server.bundle.agent.turns == ["hi"]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_events_are_pushed_to_multiple_clients(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        a = await _connected(server)
        b = await _connected(server)
        await a.request("send_message", text="hi")
        assert (await a.recv())["event"] == "TextDelta"
        assert (await b.recv())["event"] == "TextDelta"
        a.close()
        b.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_hitl_is_denied_and_visible(tmp_path):
    """host 里没人能答 ask，必须拒绝且留下痕迹——不能静默放行。"""
    server = _make_server(tmp_path)
    server.bundle.agent.script["do"] = [
        HITLRequired(
            tool_name="write_file",
            tool_use_id="t1",
            description="写文件",
            arguments={"file_path": "a"},
        ),
        AgentFinished(text="done"),
    ]
    await server.start()
    try:
        client = await _connected(server)
        await client.request("send_message", text="do")
        assert (await client.recv())["event"] == "HITLRequired"
        assert (await client.recv())["event"] == "AgentFinished"
        assert server.bundle.agent.resolved == [("t1", False, "deny")]
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_turn_failure_is_reported_not_fatal(tmp_path):
    """一个回合炸了不能掀掉 host：报错事件之后 host 仍能接活。"""

    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, text: str):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider exploded")
            yield AgentFinished(text="recovered")

        def resolve_hitl(self, *a, **k) -> None:
            pass

    server = _make_server(tmp_path)
    server.bundle.agent = _Boom()
    await server.start()
    try:
        client = await _connected(server)
        await client.request("send_message", text="first")
        err = await client.recv()
        assert err["event"] == "HostError"
        assert "provider exploded" in err["data"]["message"]

        await client.request("send_message", text="second")
        assert (await client.recv())["event"] == "AgentFinished"
        client.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_offer_drops_oldest_when_queue_full(tmp_path):
    """队列满时丢最旧的，且 `_offer` 绝不阻塞——这就是"慢客户端不拖住 agent"的全部实现。

    单独测 `_offer` 而不是走网络：走网络时事件什么时候被泵出去取决于事件循环调度，
    "丢了第几条"无法确定断言。
    """
    server = _make_server(tmp_path)
    client = SimpleNamespace(queue=asyncio.Queue(CLIENT_QUEUE_SIZE))

    for i in range(CLIENT_QUEUE_SIZE):
        server._offer(client, {"event": "E", "data": {"i": i}})
    assert client.queue.full()

    # 满了再来一条：不抛、不阻塞，丢最旧
    server._offer(client, {"event": "E", "data": {"i": -1}})
    assert client.queue.qsize() == CLIENT_QUEUE_SIZE
    assert client.queue.get_nowait()["data"]["i"] == 1
    assert client.queue.get_nowait()["data"]["i"] == 2
    # 最后一条是刚塞进去的那条
    while client.queue.qsize() > 1:
        client.queue.get_nowait()
    assert client.queue.get_nowait()["data"]["i"] == -1


async def test_burst_events_do_not_block_turn(tmp_path):
    """事件连发时回合必须跑完，不能被客户端队列拖住。

    这个桩 agent 的 301 个事件是在**一次事件循环调度里连发**的（`async for` 内部
    一个 await 都没有），必然撑爆 200 的队列上限——正是要覆盖的路径。所以这里不
    断言收到多少条事件，只断言：回合跑完了、并且还在读的那条连接最终拿到了
    `AgentFinished`（说明它没被另一条连接饿死）。
    """
    count = 300
    server = _make_server(tmp_path)
    server.bundle.agent.script["flood"] = [
        TextDelta(text="x") for _ in range(count)
    ] + [AgentFinished(text="done")]
    await server.start()
    try:
        idle = await _connected(server)  # 连上就不收事件
        active = await _connected(server)
        await active.request("send_message", text="flood")

        while (await active.recv())["event"] != "AgentFinished":
            pass

        assert server.bundle.agent.turns == ["flood"]
        assert not server.busy
        idle.close()
        active.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_client_disconnect_does_not_stop_host(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        first = await _connected(server)
        first.close()
        await asyncio.sleep(0.05)
        second = await _connected(server)
        assert (await second.request("get_run"))["ok"]
        second.close()
    finally:
        server.request_shutdown()
        await server.wait_closed()


# ── 关闭语义 ────────────────────────────────────────────────────


async def test_graceful_shutdown_marks_completed(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    server.request_shutdown()
    await server.wait_closed()
    record = server._store.get(server.run.id)
    assert record.status is RunStatus.COMPLETED
    assert record.exit_reason


async def test_shutdown_closes_bundle(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    server.request_shutdown()
    await server.wait_closed()
    assert server.bundle.closed


async def test_shutdown_with_connected_client_does_not_hang(tmp_path):
    """还有客户端连着时也必须关得掉。

    `asyncio.Server.close()` 的契约是"leaves existing connections open"，而
    `Server.wait_closed()` 等的是连接全断——顺序写反就会死锁在这里（用
    `async with self._server` 就是这个写法）。这条测试是那个死锁的回归护栏：
    `wait_for` 让"死锁"表现为失败而不是永远挂着。
    """
    server = _make_server(tmp_path)
    await server.start()
    client = await _connected(server)  # 故意不关它
    try:
        server.request_shutdown()
        await asyncio.wait_for(server.wait_closed(), 10.0)
        assert server._store.get(server.run.id).status is RunStatus.COMPLETED
    finally:
        client.close()


async def test_handler_closes_its_connection(tmp_path):
    """handler 返回时必须自己关 writer——不关的话连接永远挂着，host 关不掉。

    `asyncio.start_server` 的 done-callback 只在 handler 被**取消或抛异常**时才
    `transport.close()`，正常返回时什么都不做（见
    `StreamReaderProtocol.connection_made`）。这是个很反直觉的坑：handler 明明已经
    返回了，`Server._active_count` 却还是 1。

    断言用公开 API：客户端断开后，服务端**自己**应该已经能把 `wait_closed()` 走完。
    `wait_for` 把"漏关连接"变成 2 秒内的失败，而不是永久挂起。
    """
    server = _make_server(tmp_path)
    await server.start()
    client = await _connected(server)
    client.close()
    await asyncio.sleep(0.1)  # 让服务端 handler 看到 EOF 并收尾
    try:
        server._server.close()
        await asyncio.wait_for(server._server.wait_closed(), 2.0)
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_cancel_marks_interrupted(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    task = asyncio.create_task(server.wait_closed())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = server._store.get(server.run.id)
    assert record.status is RunStatus.INTERRUPTED
    assert "取消" in record.exit_reason


async def test_bound_to_loopback_only(tmp_path):
    server = _make_server(tmp_path)
    await server.start()
    try:
        assert server.listening
        assert server.port > 0
        for sock in server._server.sockets:
            assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        server.request_shutdown()
        await server.wait_closed()


# ── 真接线 ──────────────────────────────────────────────────────


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="Test",
        protocol="openai",
        model="gpt-4o",
        api_key="sk-test-not-a-real-key",
    )


async def test_start_host_wires_lock_and_run(tmp_path):
    server = await start_host(provider=_provider(), workspace=tmp_path)
    try:
        assert server.bundle.lock is not None
        assert server.bundle.lock.held
        assert server.listening and server.port > 0
        record = server._store.get(server.run.id)
        # start_host 返回时已经在监听，所以 run 是 running 而不是 queued
        assert record.status is RunStatus.RUNNING
        assert record.session_id == server.bundle.runtime.session.session_id
        assert record.pid == os.getpid()
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_start_host_rejects_second_active_run(tmp_path):
    """首期一个 workspace 只允许一个活跃 run——否则 send_message 的语义立刻有歧义。"""
    first = await start_host(provider=_provider(), workspace=tmp_path)
    try:
        with pytest.raises(HostAlreadyRunningError) as exc:
            await start_host(provider=_provider(), workspace=tmp_path)
        assert exc.value.run.id == first.run.id
    finally:
        first.request_shutdown()
        await first.wait_closed()


async def test_start_host_reclaims_stale_run_first(tmp_path):
    """上一个进程留下的僵尸 run 要先改判，再建自己的——顺序反了会把自己扫进去。"""
    store = RunStore(tmp_path)
    zombie = store.create("20260916-150000-dead", pid=None)
    assert store.get(zombie.id).status is RunStatus.QUEUED

    server = await start_host(provider=_provider(), workspace=tmp_path, store=store)
    try:
        assert store.get(zombie.id).status is RunStatus.INTERRUPTED
        assert store.get(server.run.id).status is RunStatus.RUNNING
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_start_host_writes_host_info(tmp_path):
    """会合信息必须在 start_host 返回时就落盘——客户端靠它找端口。"""
    server = await start_host(provider=_provider(), workspace=tmp_path)
    info = load_host_info(tmp_path)
    try:
        assert info is not None
        assert info.port == server.port
        assert info.pid == os.getpid()
        assert info.run_id == server.run.id
        assert info.protocol == protocol_mod.PROTOCOL_VERSION
    finally:
        server.request_shutdown()
        await server.wait_closed()

    # 正常关闭后清掉：再连就该说"没有正在运行的 host"
    assert load_host_info(tmp_path) is None


async def test_bind_failure_settles_run_and_releases_lock(tmp_path):
    """端口被占时启动失败，必须把刚建的 run 收尾并放掉锁。

    否则它会以 `running` 留在库里，而 pid 指向当前进程（还活着）——下一次启动被
    `latest_active()` 判为"已有活跃 run"，永远起不来。
    """
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        with pytest.raises(OSError):
            await start_host(provider=_provider(), workspace=tmp_path, port=port)
    finally:
        blocker.close()

    store = RunStore(tmp_path)
    assert [r.status for r in store.list(limit=None)] == [RunStatus.FAILED]

    # 锁也放掉了：下一次启动能正常抢到
    server = await start_host(provider=_provider(), workspace=tmp_path, store=store)
    try:
        assert server.bundle.lock is not None and server.bundle.lock.held
    finally:
        server.request_shutdown()
        await server.wait_closed()


async def test_start_host_after_shutdown_is_possible(tmp_path):
    first = await start_host(provider=_provider(), workspace=tmp_path)
    session_dir = first.bundle.runtime.session.session_dir
    first.request_shutdown()
    await first.wait_closed()

    second = await start_host(
        provider=_provider(),
        workspace=tmp_path,
        session_dir=session_dir,
        conversation=ConversationManager(),
    )
    try:
        assert second.run.id != first.run.id
        assert second.run.session_id == first.run.session_id
    finally:
        second.request_shutdown()
        await second.wait_closed()
