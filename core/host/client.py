"""host 客户端：连上正在跑的 host，发命令、收事件。

要连上一个 host 需要三样东西，都在 `<workspace>/.codeforge/`：

    host.json    往哪连 —— 端口
    host.token   凭什么连 —— 凭据
    host.json    连的是谁 —— run_id（顺便用来核对"你 attach 的和我连上的是同一个"）

**帧路由是这里唯一有讲究的地方**：应答与事件**可以交错**（服务端先推事件、后回
ack 是允许的，因为回合事件由另一个任务扇出），所以不能"发一条就同步读一条"。做法是
一条后台读循环，按帧里有没有 `id` 分流——有 `id` 且能对上待处理请求的当应答，其余
一律进事件队列。这与 `tests/test_host_protocol.py` 里的测试客户端是同一套逻辑
（那边是协议验证，这边是生产实现，两边行为必须一致）。

**host 不在时要给出可执行的下一步**，而不是一句"连接失败"。没找到 `host.json`、
里面的 pid 已退出、端口连不上——三种都归到 `HostNotRunningError`，消息里带上
`codeforge host`。这三种的处置方式对用户是同一件事，区分它们只会让调用方多写三个
`except`。

这个模块刻意**不 import `core.host.server`**：客户端不该为了连一次 host 就把整个
agent 装配栈拉起来（`server` → `core.agent.bootstrap` → 工具注册表 → 各 provider）。
loopback 地址与默认端口因此住在 `protocol.py`。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from core.host.proc import pid_alive
from core.host.protocol import (
    HOST_BIND_ADDRESS,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_frame,
    encode_frame,
    load_host_info,
    load_token,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from core.host.protocol import HostInfo
    from core.host.run_store import RunStatus

# 连上（含握手）的总时限。loopback 上正常是毫秒级，5 秒足够区分"卡住"与"慢"
CONNECT_TIMEOUT_SECONDS = 5.0

# 单条命令的应答时限。命令由 host 的独立任务处理，不排在回合后面，所以总是很快；
# 给到 30 秒只是为了容忍宿主机负载抖动。
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class HostNotRunningError(RuntimeError):
    """没有可连的 host：没启动 / 已退出 / 端口连不上。

    三种情况合并成一个异常，因为对调用方的处置是同一件事——提示用户先起 host。
    具体原因放在 `detail` 里，供消息与日志使用。
    """

    def __init__(self, workspace: Path, detail: str) -> None:
        super().__init__(
            f"没有正在运行的 host（{detail}）。"
            f"先在 {workspace} 下运行 codeforge host，再用 codeforge attach 查看。"
        )
        self.workspace = workspace
        self.detail = detail


class HostError(RuntimeError):
    """host 应答了 `ok=false`。"""

    def __init__(self, message: str, *, cmd: str | None = None) -> None:
        super().__init__(message)
        self.cmd = cmd


class HostClient:
    """一条到 host 的连接。

    用 `await HostClient.connect(workspace)` 构造。也可以当异步上下文管理器用：

        async with await HostClient.connect(ws) as client:
            print(await client.get_run())
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        workspace: Path,
        info: HostInfo,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._workspace = workspace
        self._info = info
        self._counter = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop())

    # ── 连接 ────────────────────────────────────────────────────

    @classmethod
    async def connect(
        cls,
        workspace: str | Path,
        *,
        timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> HostClient:
        """连上该工作区的 host 并完成握手。

        Raises:
            HostNotRunningError: 找不到 host / host 已退出 / 端口连不上。
            HostError: 握手被拒（token 或协议版本不对）。
        """
        ws = Path(workspace).resolve()

        info = load_host_info(ws)
        if info is None:
            raise HostNotRunningError(ws, "没有找到 .codeforge/host.json")
        # 崩溃的 host 会留下过期的 host.json。先按 pid 判活，避免把"端口连不上"
        # 这种含糊的原因报给用户——真实原因是那个进程已经不在了。
        if not pid_alive(info.pid):
            raise HostNotRunningError(ws, f"host.json 记录的进程 {info.pid} 已退出")

        token = load_token(ws)
        if token is None:
            raise HostNotRunningError(ws, "没有找到 .codeforge/host.token")

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(HOST_BIND_ADDRESS, info.port), timeout
            )
        except (OSError, TimeoutError) as e:
            raise HostNotRunningError(
                ws, f"连不上 {HOST_BIND_ADDRESS}:{info.port}（{e}）"
            ) from e

        client = cls(reader, writer, workspace=ws, info=info)
        try:
            await client._handshake(token, timeout=timeout)
        except BaseException:
            # 握手失败也必须把连接与读循环收掉，否则每失败一次漏一条连接 + 一个任务
            client.close()
            raise
        return client

    async def _handshake(self, token: str, *, timeout: float) -> None:
        try:
            resp = await self._exchange(
                {"cmd": "hello", "token": token, "protocol": PROTOCOL_VERSION},
                timeout=timeout,
            )
        except TimeoutError as e:
            # 端口上有人听但不应答：多半那个进程根本不是 codeforge host（或者它卡死
            # 了）。裸 TimeoutError 对用户没有信息量，转成带原因的错误。
            raise HostError(f"握手超时（{timeout}s 内没有应答）", cmd="hello") from e
        if not resp.get("ok"):
            raise HostError(resp.get("error") or "握手被拒绝", cmd="hello")
        data = resp.get("data") or {}
        proto = data.get("protocol")
        if proto != PROTOCOL_VERSION:
            # 服务端已经查过一遍版本，这里是防"服务端漏查"的兜底。两层都要有：
            # 只查服务端，客户端拿到新字段会静默按错的方式解释。
            raise HostError(
                f"协议版本不匹配：host 是 {proto!r}，客户端是 {PROTOCOL_VERSION}",
                cmd="hello",
            )

    # ── 只读属性 ────────────────────────────────────────────────

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def info(self) -> HostInfo:
        return self._info

    @property
    def run_id(self) -> str:
        return self._info.run_id

    @property
    def closed(self) -> bool:
        return self._closed

    # ── 请求 / 应答 ─────────────────────────────────────────────

    async def _exchange(
        self, payload: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """发一帧并等它的应答。`payload` 不含 `id`——由这里分配。"""
        if self._closed:
            raise ConnectionError("host 连接已断开")

        self._counter += 1
        req_id = str(self._counter)
        fut: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[req_id] = fut
        try:
            self._writer.write(encode_frame({"id": req_id, **payload}))
            await self._writer.drain()
            return await asyncio.wait_for(fut, timeout)
        finally:
            # 超时时 `wait_for` 会 cancel 掉 fut；这里必须摘掉，否则 _pending 会
            # 随着每次超时无限增长
            self._pending.pop(req_id, None)

    async def _call(
        self, cmd: str, *, timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS, **args: Any
    ) -> dict[str, Any]:
        return await self._exchange({"cmd": cmd, "args": args}, timeout=timeout)

    async def call(
        self, cmd: str, *, timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS, **args: Any
    ) -> Any:
        """发一条命令，返回应答里的 `data`。

        Raises:
            HostError: 应答是 `ok=false`。
        """
        resp = await self._call(cmd, timeout=timeout, **args)
        if not resp.get("ok"):
            raise HostError(resp.get("error") or f"{cmd} 失败", cmd=cmd)
        return resp.get("data")

    # ── 命令封装 ────────────────────────────────────────────────

    async def list_runs(
        self, *, limit: int = 50, status: RunStatus | str | None = None
    ) -> dict[str, Any]:
        """列出 run。返回 `{"runs": [...], "active_run_id": ...}`。"""
        args: dict[str, Any] = {"limit": limit}
        if status is not None:
            args["status"] = status.value if hasattr(status, "value") else status
        return await self.call("list_runs", **args)

    async def get_run(self, run_id: str | None = None) -> dict[str, Any]:
        """查一个 run 的状态；不传 `run_id` 即当前活跃 run。"""
        return await self.call("get_run", **({"run_id": run_id} if run_id else {}))

    async def read_conversation(
        self, run_id: str | None = None, *, limit: int = 50
    ) -> dict[str, Any]:
        """读对话。返回 `{"run_id", "session_id", "total", "messages": [...]}`。"""
        args: dict[str, Any] = {"limit": limit}
        if run_id:
            args["run_id"] = run_id
        return await self.call("read_conversation", **args)

    async def send_message(self, text: str) -> dict[str, Any]:
        """投一条用户输入。首期单活跃 run，故隐式作用于它。"""
        return await self.call("send_message", text=text)

    # ── 事件流 ──────────────────────────────────────────────────

    async def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        """取下一条事件；连接已结束且事件已取完时返回 `None`。

        结束判定是**粘性**的（先看队列空不空，再看 `_closed`），而不是"队列里那条
        `None` 哨兵"。用哨兵会有个很隐蔽的错：哨兵是一次性的，谁先取到谁就结束，
        第二个消费者（或者 `events()` 循环之后的又一次 `next_event()`）会在一个
        空队列上永远等下去。这里靠状态而不是靠队列内容，重复调用都得到 `None`。

        Raises:
            TimeoutError: 给了 `timeout` 且在时限内没有事件。
        """
        if self._events.empty() and self._closed:
            return None
        if timeout is None:
            frame = await self._events.get()
        else:
            frame = await asyncio.wait_for(self._events.get(), timeout)
        # 队列里可能还留着终止哨兵，它也是 None——对调用方是同一个意思
        return frame

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """一直产出事件帧，直到连接结束。"""
        while True:
            frame = await self.next_event()
            if frame is None:
                return
            yield frame

    # ── 收尾 ────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """后台读帧，按 `id` 分流：能对上待处理请求的当应答，其余当事件。"""
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    frame = decode_frame(line)
                except ProtocolError:
                    # 坏帧不该毁掉客户端。服务端只在"帧根本解不开"时才会发这种
                    # 无 id 的应答，丢掉即可，继续读。
                    continue
                fut = self._pending.get(str(frame.get("id")))
                if fut is not None and not fut.done():
                    fut.set_result(frame)
                else:
                    self._events.put_nowait(frame)
        except (ConnectionError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            # 顺序要紧：先立 `_closed`（让 `next_event` 的粘性结束判定成立），再补哨兵
            # 唤醒此刻正阻塞在 `_events.get()` 上的消费者。
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("host 连接已断开"))
            self._pending.clear()
            self._events.put_nowait(None)

    def close(self) -> None:
        """关连接（幂等）。不等 flush——收尾时不该为一个不读的对端等下去。"""
        self._closed = True
        self._reader_task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()
