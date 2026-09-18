"""host 进程：把会话从终端进程里搬出来独立跑。

它要解决的是「关掉终端，会话就没了」。host 自己持有会话（装配、写
`conversation.jsonl`、持单写者锁），客户端只通过 loopback 控制通道看它、喂它。

一个 host 进程的结构：

    ┌─ 会话（SessionBundle）─── agent / conversation / writer / journal / lock
    ├─ 回合队列（_inbox）───── 客户端 send_message 投进来的用户输入
    ├─ 事件扇出（_subscribers）─ 每个客户端一条队列，慢客户端丢事件不阻塞 agent
    └─ 控制通道（TCP loopback）─ list_runs / get_run / read_conversation / send_message

**首期一个 workspace 只允许一个活跃 run**（spec「Out of Scope」）。`start_host()`
发现已有活跃 run 就拒绝启动——否则 `send_message` 的"隐式作用于当前活跃 run"
这条约定会立刻变得有歧义。

**关闭语义**：正常关闭写 `completed`，被取消/异常写 `interrupted`。关闭时会取消
正在跑的回合——一个回合可能跑几分钟，等它跑完就不叫"关得掉"了。代价是可能留下
未配对的 `tool_use`，而这正是 journal + recovery（任务 6/7）存在的理由：下次
`attach --continue` 会把"可能已生效"的调用点列出来，不会盲目重跑。

**关闭顺序**（`_teardown`）：停监听 → 停回合循环 → **关客户端** → 关会话 → 等
`Server.wait_closed()`。中间那步不能省也不能挪到后面——`asyncio.Server.close()`
的契约是"leaves existing connections open"，而 `wait_closed()` 等的是所有客户端
连接都断开，顺序反了就会死锁在一个连着的客户端上。

**临时处置**：host 里 `HITLRequired` 无人可答，本模块一律 `resolve_hitl(deny)` 并把
事件推给客户端（原因写明"host 模式暂无人工确认通道"）。这是**保守占位**——宁可拒绝
也不静默放行。真正的无人值守策略由任务 14 落地。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conversation.manager import ConversationManager
from core.agent.bootstrap import SessionBundle, build_session
from core.agent.events import HITLRequired
from core.archive.reader import read_messages
from core.archive.writer import CONVERSATION_FILENAME
from core.host.lifecycle import RunLifecycle
from core.host.protocol import (
    DEFAULT_PORT,
    HOST_BIND_ADDRESS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    HostInfo,
    ProtocolError,
    clear_host_info,
    decode_frame,
    encode_frame,
    event_frame,
    issue_token,
    token_matches,
    write_host_info,
)
from core.host.run_store import RunRecord, RunStatus, RunStore

logger = logging.getLogger(__name__)

# 每个客户端的事件积压上限。满了丢最旧的——一个卡住的客户端不能拖住 agent。
CLIENT_QUEUE_SIZE = 200

# 供回看的最近事件条数
EVENT_BACKLOG_SIZE = 200

# 读会话时的默认条数
DEFAULT_CONVERSATION_LIMIT = 50

# 关闭时等客户端连接断开的上限。等不到就强制 abort——见 `_teardown`。
CLOSE_TIMEOUT_SECONDS = 5.0


class HostAlreadyRunningError(RuntimeError):
    """同一 workspace 已有活跃 run，拒绝再起一个 host。"""

    def __init__(self, run: RunRecord) -> None:
        self.run = run
        super().__init__(
            f"该工作区已有活跃 run（{run.id}，pid={run.pid}，状态={run.status.value}）。"
            "首期一个工作区只允许一个活跃 run；先 codeforge attach 查看，"
            "或等它结束/用 codeforge runs 确认它是僵尸后再启动。"
        )


@dataclass(eq=False)
class _Client:
    """一条客户端连接。

    `eq=False` 是必需的：默认的 `@dataclass` 会生成 `__eq__` 并把 `__hash__` 置为
    `None`，于是 `self._clients.add(client)` 抛 `TypeError: unhashable type`。
    而且连接的身份就该是对象身份——两条连接即使字段全等也是不同的客户端。
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(CLIENT_QUEUE_SIZE)
    )
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, payload: dict[str, Any]) -> None:
        """写一帧。多路写入（应答 + 事件推送）靠锁串行，避免帧交错。"""
        async with self.write_lock:
            self.writer.write(encode_frame(payload))
            await self.writer.drain()

    def close(self) -> None:
        """先礼：`close()` 会把缓冲里没写完的帧发完再断。"""
        with contextlib.suppress(Exception):
            self.writer.close()

    def abort(self) -> None:
        """后兵：直接丢弃缓冲断开。

        客户端不读时 TCP 窗口会满，`close()` 会一直等缓冲写出去——永远等不到。关闭
        路径上不能为一个卡住的客户端无限等下去，所以有这一手。
        """
        with contextlib.suppress(Exception):
            self.writer.transport.abort()


def _message_entry(msg: Any) -> dict[str, Any]:
    """把一条 Message 转成给客户端看的形状。"""
    return {
        "role": msg.role.value,
        "content": msg.content,
        "tool_name": msg.tool_name,
        "tool_use_id": msg.tool_use_id,
        "timestamp": msg.timestamp,
    }


class HostServer:
    """承载一个会话的 host。

    用 `start_host()` 构造——它负责装配会话、抢锁、建 run 并开始监听，返回的对象
    已经在跑。直接 `HostServer(...)` 只在测试里用（可以自己控 `start()` 的时机）。
    """

    def __init__(
        self,
        bundle: SessionBundle,
        *,
        store: RunStore,
        run: RunRecord,
        lifecycle: RunLifecycle,
        port: int = DEFAULT_PORT,
        host: str = HOST_BIND_ADDRESS,
    ) -> None:
        self._bundle = bundle
        self._workspace = Path(bundle.workspace)
        self._store = store
        self._run = run
        self._lifecycle = lifecycle
        self._host = host
        self._port = port

        # token 在构造期就换新：上一个 host 的凭据立刻失效
        self._token = issue_token(self._workspace)

        self._server: asyncio.AbstractServer | None = None
        self._worker: asyncio.Task | None = None
        self._clients: set[_Client] = set()
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self._backlog: deque[dict[str, Any]] = deque(maxlen=EVENT_BACKLOG_SIZE)
        self._stop = asyncio.Event()
        self._busy = False

    # ── 对外只读 ────────────────────────────────────────────────

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def run(self) -> RunRecord:
        return self._run

    @property
    def bundle(self) -> SessionBundle:
        return self._bundle

    @property
    def port(self) -> int:
        """实际监听端口（`port=0` 时由内核分配，监听后才能知道）。"""
        if self._server is not None:
            for sock in self._server.sockets or ():
                return int(sock.getsockname()[1])
        return self._port

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def recent_events(self) -> list[dict[str, Any]]:
        """最近事件（供 `attach` 打印进度摘要）。"""
        return list(self._backlog)

    # ── 生命周期 ────────────────────────────────────────────────

    async def start(self) -> None:
        """绑定端口、起回合循环。**不阻塞**。

        与 `wait_closed()` 分开是为了让调用方能先拿到实际端口（`port=0` 时由内核
        分配，只有绑定后才知道）再打印/上报，而不是先阻塞住。

        绑定失败（端口被占等）时必须把刚建的 run 落成 `failed` 并放掉锁：run 已经
        是 `running` 且 pid 指向当前进程（还活着），留着它会让下一次启动被
        `latest_active()` 判为"已有活跃 run"而永远起不来。
        """
        self._lifecycle.start(self._run.id)
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self._host, self._port, limit=MAX_FRAME_BYTES
            )
        except Exception as e:
            self._settle(RunStatus.FAILED, f"监听失败: {e}")
            self._bundle.close()
            raise
        # 端口只有绑完才知道，所以会合信息必须在这里落盘（客户端靠它找过来）
        write_host_info(
            self._workspace,
            HostInfo(port=self.port, pid=os.getpid(), run_id=self._run.id),
        )
        self._worker = asyncio.create_task(self._turn_worker())

    async def wait_closed(self) -> None:
        """跑到收到关闭信号，落终态并清理。

        被 `kill -9` 时什么都来不及写，库里的 `running` 由下一次启动的
        `mark_stale_interrupted()` 收尾——这也是 stale 改判必须存在的原因。
        """
        if self._server is None:
            raise RuntimeError("wait_closed() 前必须先 start()")
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            self._settle(RunStatus.INTERRUPTED, "host 被取消")
            raise
        except Exception as e:
            self._settle(RunStatus.INTERRUPTED, f"host 异常退出: {e}")
            raise
        else:
            self._settle(RunStatus.COMPLETED, "host 正常关闭")
        finally:
            await self._teardown()

    async def serve(self) -> None:
        """`start()` + `wait_closed()`。"""
        await self.start()
        await self.wait_closed()

    async def _teardown(self) -> None:
        """停监听 → 停回合循环 → 关客户端 → 关会话。

        **顺序是这里唯一重要的事**：`asyncio.Server.close()` 的契约是"leaves existing
        connections open"，而 `Server.wait_closed()` 等的是"所有客户端连接都断开"。
        所以必须先把客户端关掉再去 await `wait_closed()`——反过来（比如用
        `async with self._server`）只要还有一个客户端连着就永远等不完。这不是理论
        问题：一个连着但不读事件的 `attach` 就足以让 Ctrl-C 关不掉 host。
        """
        server = self._server
        self._server = None
        if server is not None:
            server.close()  # 先不再接受新连接

        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

        clients = list(self._clients)
        self._clients.clear()
        for c in clients:
            c.close()
        self._bundle.close()
        # 会合信息最后删，且只删自己那份：删早了客户端会以为 host 没了，删错了会把
        # 下一个 host 的信息删掉
        clear_host_info(self._workspace, pid=os.getpid())

        if server is None:
            return
        try:
            await asyncio.wait_for(server.wait_closed(), CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("关闭超时：强制断开 %d 个连接", len(clients))
            for c in clients:
                c.abort()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), CLOSE_TIMEOUT_SECONDS)

    def request_shutdown(self) -> None:
        """请求优雅关闭（幂等）。"""
        self._stop.set()

    def _settle(self, status: RunStatus, reason: str) -> None:
        """写终态；已经落过终态就不覆盖。"""
        current = self._store.get(self._run.id)
        if current is None or current.is_terminal:
            return
        with contextlib.suppress(Exception):
            if status is RunStatus.COMPLETED:
                self._lifecycle.complete(self._run.id, reason=reason)
            elif status is RunStatus.FAILED:
                self._lifecycle.fail(self._run.id, reason=reason)
            else:
                self._lifecycle.interrupt(self._run.id, reason=reason)

    # ── 回合循环 ────────────────────────────────────────────────

    async def _turn_worker(self) -> None:
        """串行消费收件箱。一个 host 同时只跑一个回合。"""
        while True:
            text = await self._inbox.get()
            if text is None:
                return
            await self._run_turn(text)

    async def _run_turn(self, text: str) -> None:
        self._busy = True
        try:
            async for event in self._bundle.agent.run(text):
                if isinstance(event, HITLRequired):
                    await self._deny_hitl(event)
                await self._publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 单回合失败不该掀掉整个 host
            logger.exception("host 回合失败")
            await self._publish_error(str(e))
        finally:
            self._busy = False

    async def _deny_hitl(self, event: HITLRequired) -> None:
        """兜底：没配无人值守策略时的保守处置（一律拒绝）。

        正常情况下 `ask` 级决策在权限层就被 `UnattendedPolicy` 代答了，
        这里收不到事件。只有当 host 既没给策略、又确实产生了需要人决策的
        调用时才会走到——拒绝是可观测、可解释的，静默放行则会在无人值守时
        把写权限交出去而没有任何记录。
        """
        self._bundle.agent.resolve_hitl(event.tool_use_id, False, "deny")
        logger.warning(
            "host 未配置无人值守策略，保守拒绝 %s（如需放行请用 --unattended-policy）",
            event.tool_name,
        )

    async def _publish(self, event: Any) -> None:
        frame = event_frame(event)
        self._backlog.append(frame)
        for client in list(self._clients):
            self._offer(client, frame)

    async def _publish_error(self, message: str) -> None:
        frame = {"event": "HostError", "data": {"message": message}}
        self._backlog.append(frame)
        for client in list(self._clients):
            self._offer(client, frame)

    @staticmethod
    def _offer(client: _Client, frame: dict[str, Any]) -> None:
        """非阻塞投递；队列满就丢最旧的。

        宁可让慢客户端漏事件，也不能让 `await queue.put()` 把 agent 的回合卡住
        ——事件是"看进度"用的，进度显示不完整远比任务停摆可接受。
        """
        try:
            client.queue.put_nowait(frame)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                client.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                client.queue.put_nowait(frame)

    # ── 控制通道 ────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = _Client(reader=reader, writer=writer)
        try:
            if not await self._handshake(client):
                return
            self._clients.add(client)
            await self._serve_client(client)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        except (ValueError, ProtocolError) as e:
            # 帧超长（ValueError）或帧非法：断开这一条即可，不掀掉 host
            logger.debug("客户端连接中断: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 一条连接的意外错误只该断它自己。这里兜底是为了：既保证下面 finally 一定
            # 跑到，也不让异常冒到事件循环变成"Task exception was never retrieved"。
            # （本仓库启用的规则集里没有 BLE001，故不加 noqa）
            logger.exception("客户端处理异常，断开该连接")
        finally:
            self._clients.discard(client)
            # 这个 close() **不是可选的收尾礼仪，是正确性要求**：
            # `StreamReaderProtocol.connection_made` 的 done-callback 只在 handler 被
            # 取消或抛异常时才 `transport.close()`，**正常返回时什么都不做**。所以
            # handler 不自己关 writer，连接就永远挂着，`Server.wait_closed()` 再也等
            # 不到 `_active_count` 归零——表现是 host 关不掉。
            client.close()

    async def _handshake(self, client: _Client) -> bool:
        """首帧必须是带正确 token 的 `hello`。失败即断开且不透露任何数据。

        拒绝帧也带上 `id`：客户端靠 `id` 关联应答，不带 `id` 的拒绝帧会被当成
        事件推送，客户端只能看到"连接被关"而看不到原因。
        """
        try:
            line = await client.reader.readline()
        except ValueError:
            return False
        if not line:
            return False
        try:
            req = decode_frame(line)
        except ProtocolError:
            # 帧都解不开，自然拿不到 id
            await client.send({"ok": False, "error": "首帧必须是 JSON 对象"})
            return False

        req_id = req.get("id")
        if req.get("cmd") != "hello":
            await client.send(
                {"id": req_id, "ok": False, "error": "首帧必须是 hello 握手"}
            )
            return False
        if not token_matches(self._token, req.get("token")):
            # 不回显 token，也不区分"没带"与"带错"
            await client.send({"id": req_id, "ok": False, "error": "token 校验失败"})
            return False
        # 版本不匹配时当场说清楚，别让对方连上之后栽在一堆 KeyError 上
        proto = req.get("protocol")
        if proto != PROTOCOL_VERSION:
            await client.send(
                {
                    "id": req_id,
                    "ok": False,
                    "error": f"协议版本不匹配：host 是 {PROTOCOL_VERSION}，客户端是 {proto!r}",
                }
            )
            return False

        await client.send(
            {
                "id": req_id,
                "ok": True,
                "data": {
                    "protocol": PROTOCOL_VERSION,
                    "workspace": str(self._workspace),
                    "run": self._run.to_dict(),
                    "busy": self._busy,
                },
            }
        )
        return True

    async def _serve_client(self, client: _Client) -> None:
        pump = asyncio.create_task(self._pump_events(client))
        try:
            while True:
                line = await client.reader.readline()
                if not line:
                    return
                try:
                    req = decode_frame(line)
                except ProtocolError as e:
                    await client.send({"ok": False, "error": str(e)})
                    continue
                await client.send(await self._dispatch(req))
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    async def _pump_events(self, client: _Client) -> None:
        while True:
            frame = await client.queue.get()
            await client.send(frame)

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        """执行一条命令，返回应答帧。异常一律转成 `ok=false`，不让它掀掉连接。"""
        req_id = req.get("id")
        cmd = req.get("cmd")
        args = req.get("args") or {}
        if not isinstance(args, dict):
            return {"id": req_id, "ok": False, "error": "args 必须是对象"}
        handler = _COMMANDS.get(cmd) if isinstance(cmd, str) else None
        if handler is None:
            return {"id": req_id, "ok": False, "error": f"未知命令: {cmd!r}"}
        try:
            return {"id": req_id, "ok": True, "data": await handler(self, args)}
        except Exception as e:  # 命令失败只影响这一条应答
            logger.exception("命令 %s 执行失败", cmd)
            return {"id": req_id, "ok": False, "error": str(e)}

    # ── 命令实现 ────────────────────────────────────────────────

    async def _cmd_list_runs(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 50)
        status_raw = args.get("status")
        status = RunStatus(status_raw) if status_raw else None
        records = self._store.list(limit=limit, status=status)
        return {
            "runs": [r.to_dict() for r in records],
            "active_run_id": self._run.id,
        }

    async def _cmd_get_run(self, args: dict[str, Any]) -> dict[str, Any]:
        run_id = str(args.get("run_id") or self._run.id)
        record = self._store.get(run_id)
        if record is None:
            raise LookupError(f"run 不存在: {run_id}")
        data = record.to_dict()
        is_active = record.id == self._run.id
        data["is_active"] = is_active
        data["busy"] = self._busy if is_active else False
        return data

    async def _cmd_read_conversation(self, args: dict[str, Any]) -> dict[str, Any]:
        """读对话。

        活跃 run 直接读内存（比磁盘更新）；非活跃 run 只能读磁盘上的 JSONL
        ——那条路径上没有 `ConversationManager`。
        """
        run_id = str(args.get("run_id") or self._run.id)
        limit = int(args.get("limit") or DEFAULT_CONVERSATION_LIMIT)
        record = self._store.get(run_id)
        if record is None:
            raise LookupError(f"run 不存在: {run_id}")

        if record.id == self._run.id:
            msgs = self._bundle.conversation.messages
        else:
            session_dir = (
                self._workspace / ".codeforge" / "sessions" / record.session_id
            )
            msgs, _skipped, _ts = read_messages(session_dir / CONVERSATION_FILENAME)

        entries = [_message_entry(m) for m in msgs]
        return {
            "run_id": record.id,
            "session_id": record.session_id,
            "total": len(entries),
            "messages": entries[-limit:] if limit > 0 else entries,
        }

    async def _cmd_send_message(self, args: dict[str, Any]) -> dict[str, Any]:
        """投一条用户输入。首期单活跃 run，故隐式作用于它。"""
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 不能为空")
        if self._stop.is_set():
            raise RuntimeError("host 正在关闭，不再接受新消息")
        self._inbox.put_nowait(text)
        return {"accepted": True, "queued": self._inbox.qsize(), "busy": self._busy}


# 命令表：模块级字典直接引用未绑定方法，`_dispatch` 用 `handler(self, args)` 调用。
# 不用类属性：类体里写会让每个命令方法看起来像静态方法。
_COMMANDS: dict[str, Any] = {
    "list_runs": HostServer._cmd_list_runs,
    "get_run": HostServer._cmd_get_run,
    "read_conversation": HostServer._cmd_read_conversation,
    "send_message": HostServer._cmd_send_message,
}


async def start_host(
    *,
    provider,
    workspace: str | Path,
    session_dir: str | Path | None = None,
    conversation: ConversationManager | None = None,
    loop_spec: str = "",
    unattended_policy: str | None = None,
    port: int = DEFAULT_PORT,
    host: str = HOST_BIND_ADDRESS,
    config_path: str = "config.yaml",
    store: RunStore | None = None,
) -> HostServer:
    """装配会话 + 建 run + 起 host（返回时已在监听）。

    返回前就 `start()` 掉，而不是把"起监听"留给调用方：调用方（CLI）拿到对象后要
    立刻读 `port` 打印，忘了 `start()` 的话 `port` 是 0、`wait_closed()` 直接抛错
    ——把两步合成一步，这类错误就不存在了。

    顺序有讲究：先改判上一个进程留下的僵尸 run，再建自己的 run。反过来的话自己的
    run 会被当成"上次崩溃残留"扫进去。

    Raises:
        HostAlreadyRunningError: 同一 workspace 已有活跃 run。
        core.host.lock.SessionLockedError: 会话已被其他进程占用。
    """
    ws = Path(workspace)
    run_store = store or RunStore(ws)

    lifecycle = RunLifecycle(run_store)
    stale = lifecycle.mark_stale_interrupted()
    if stale:
        logger.info("启动时改判 %d 个僵尸 run 为 interrupted", len(stale))

    active = run_store.latest_active()
    if active is not None:
        raise HostAlreadyRunningError(active)

    bundle = await build_session(
        provider=provider,
        workspace=ws,
        loop_spec=loop_spec,
        unattended_policy=unattended_policy,
        session_dir=session_dir,
        conversation=conversation,
        lock_session=True,
        config_path=config_path,
    )

    run = run_store.create(bundle.runtime.session.session_id)
    server = HostServer(
        bundle,
        store=run_store,
        run=run,
        lifecycle=lifecycle,
        port=port,
        host=host,
    )
    await server.start()
    return server
