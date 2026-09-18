"""TUI 的 host 客户端模式（`features.host.enabled=true`）。

默认（`enabled=false`）时 TUI 自己装配会话、自己跑 agent——进程活着，会话才活着。
打开这个开关后 TUI 换一种活法：**它不再是会话的主人，只是一个客户端**。

为什么不是「TUI 持有一个 bundle，同时又连 host」：那会有两个东西同时驱动同一个
agent（TUI 的交互循环与 host 的回合循环），TUI 侧还会去抢单写者锁。所以开关打开后
TUI **完全不装配会话**，一切都走控制通道。

两条路径：

    * 本工作区已有 host 在跑（`codeforge host` 起的）→ 直接连它。**只有这一种形态
      能"关掉终端会话仍在"**：会话在那个独立进程里。
    * 没有 host → 在**本进程内**起一个内嵌 host，再连它。会话仍归 host 持有
      （run 记录、单写者锁、`codeforge runs` / `attach` 都成立），但内嵌 host 随本
      进程退出而结束，所以它**抗不了关终端**。这是任务书要求的回落，不是它的卖点，
      启动时会明确提示，免得用户以为关掉窗口会话还在。

代价要说清楚：客户端模式下 TUI 拿不到 `SessionBundle`，因此**斜杠命令基本不可用**
（`/resume`、`/model`、`/team` 等都要直接操作 agent / conversation / task_mgr）。
这里只保留 `/quit`、`/status`、`/help`，其余明确回一句「host 模式下不可用」并指向
`features.host.enabled=false`——静默失效比明确不支持更糟。

渲染复用 `tui.app._StreamRenderer`，不重写一份流式渲染：它要的四个回调都能从事件帧
的 `data` 里直接取到，因为 `protocol.event_frame` 用 `to_jsonable` 把 dataclass 按
字段名展开了。跨模块用私有名不是疏忽——`tests/test_tui_stall.py` 已经这么用，而重写
一份渲染器意味着两处会各自漂移。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

from core.host import HostClient, HostError, HostNotRunningError

# 本地命令：不发给 host，由客户端自己处理
_QUIT_COMMANDS = {"/quit", "/exit", "/q"}

# 回合结束帧：`_run_turn` 每跑完一个回合恰好推一个（正常结束 AgentFinished、
# agent 报错 AgentError、回合整体抛异常 HostError），客户端靠它判断"这轮完了"。
_TURN_END_EVENTS = {"AgentFinished", "AgentError", "HostError"}


def _print_banner(console: Console, client: HostClient, *, embedded: bool) -> None:
    """连上之后告诉用户"连的是谁"，以及这个形态能做什么、不能做什么。"""
    info = client.info
    console.print("[bold cyan]CodeForge[/] [white]host 客户端模式[/]")
    console.print(f"[dim]workspace: {client.workspace}[/]")
    console.print(f"[dim]run      : {info.run_id}[/]")
    console.print(f"[dim]host     : 127.0.0.1:{info.port}（pid {info.pid}）[/]")
    if embedded:
        console.print(
            "[yellow]注意：[/]本进程内嵌了 host，**关掉这个终端会话就结束了**。"
            "想要关掉终端后会话继续，先在另一个终端跑 `codeforge host` 再连。"
        )
    else:
        console.print("[green]会话由独立 host 进程承载，断开/关终端不影响它继续跑。[/]")
    console.print(
        "[dim]斜杠命令仅 /quit /status /help（其余需要本地会话，host 模式下不可用）。"
        "要完整 TUI 功能请把 features.host.enabled 设为 false。[/]"
    )
    console.print()


async def _connect_or_embed(
    console: Console,
    workspace: Path,
    *,
    providers: list[Any],
    policy: str,
    port: int,
) -> tuple[HostClient, Any | None]:
    """连本工作区的 host；没有就内嵌一个再连。

    Returns:
        `(client, embedded_server)`。`embedded_server` 非 `None` 表示 host 是本进程
        起的，调用方收尾时必须把它关掉——否则留下一个持着锁、状态永远 `running`
        的 run。

    Raises:
        SystemExit: 连不上且无法内嵌（凭据被拒 / 没有 provider / 锁被占）。
    """
    try:
        return await HostClient.connect(workspace), None
    except HostNotRunningError as e:
        # **只有"没有 host"才回落。** 握手被拒（HostError）说明 host 明明在跑，
        # 只是我们凭据/版本不对；这时去内嵌会撞上 HostAlreadyRunningError，把
        # "token 不对"这个真因盖成"已有活跃 run"，用户会去查完全错误的方向。
        console.print(f"[dim]没有运行中的 host（{e.detail}），在本进程内嵌一个…[/]")
    except HostError as e:
        raise SystemExit(f"连上了 host 但握手被拒：{e}") from e

    if not providers:
        raise SystemExit("config.yaml 里没有可用的 provider，无法内嵌 host")

    from core.host.lock import SessionLockedError
    from core.host.server import HostAlreadyRunningError, start_host

    provider = providers[0]
    console.print(
        f"[dim]内嵌 host 使用 provider={getattr(provider, 'name', '?')}"
        f"（无人值守策略 {policy}）[/]"
    )
    try:
        server = await start_host(
            provider=provider,
            workspace=workspace,
            unattended_policy=policy,
            port=port,
        )
    except HostAlreadyRunningError as e:
        raise SystemExit(f"内嵌 host 失败：{e}") from e
    except SessionLockedError as e:
        raise SystemExit(f"内嵌 host 失败，会话已被其他进程占用：{e}") from e

    try:
        client = await HostClient.connect(workspace)
    except (HostNotRunningError, HostError) as e:
        # 刚起的 host 自己都连不上，是环境问题（防火墙拦 loopback 等）。必须把内嵌的
        # host 关掉，否则它带着锁和 running 的 run 活到进程结束。
        server.request_shutdown()
        await server.wait_closed()
        raise SystemExit(f"内嵌 host 已启动但连不上：{e}") from e
    return client, server


def _render_frame(renderer: Any, console: Console, name: str, data: dict) -> bool:
    """渲染一帧事件；返回 `True` 表示这是本回合的结束帧。"""
    if name == "TextDelta":
        renderer.on_text(str(data.get("text") or ""))
    elif name == "ThinkingDelta":
        renderer.on_thinking(str(data.get("text") or ""))
    elif name == "ToolCallStarted":
        renderer.on_tool_start(str(data.get("name") or ""), data.get("input") or {})
    elif name == "ToolCallFinished":
        renderer.on_tool_finish(
            str(data.get("name") or ""),
            data.get("input") or {},
            bool(data.get("success")),
            str(data.get("result_preview") or ""),
            int(data.get("duration_ms") or 0),
        )
    elif name == "HITLRequired":
        # host 里没有人可以问：权限层已按无人值守策略代答（没配策略则兜底拒绝）。
        # 客户端**没有**代答通道，所以这里只报告"发生过一次决策"，不提供交互——
        # 假装能确认会让用户以为自己的选择生效了。
        console.print(
            f"[yellow]需要人工确认的工具 {data.get('tool_name')}："
            "host 已按无人值守策略处理[/]"
        )
    elif name == "CompactEvent":
        console.print(f"[dim]上下文压缩：{data.get('phase')}[/]")
    elif name == "AgentFinished":
        console.print(
            f"\n[dim]--- 回合结束（{data.get('iterations', 0)} 轮，"
            f"{data.get('elapsed_s', 0)}s）[/]"
        )
        return True
    elif name == "AgentError":
        console.print(
            f"\n[red]回合出错：{data.get('message') or data.get('code')}[/]"
        )
        return True
    elif name == "HostError":
        console.print(f"\n[red]host 回合失败：{data.get('message')}[/]")
        return True
    return False


def _expected_turn_ends(ack: dict) -> int:
    """投递成功后还要看到几个"回合结束"帧。

    `ack` 是 `send_message` 的应答 `{accepted, queued, busy}`：`busy` 表示有一个回合
    正在跑，`queued` 是投递后收件箱里排队的条数（含本条）。每个回合恰好产出一个结束帧
    （正常 `AgentFinished`、agent 报错 `AgentError`、回合整体抛异常 `HostError`），
    所以条数就是"要等的回合数"。

    `max(1, ...)` 兜住一个真实竞态：回合 worker 可能在我们 `put_nowait` 之后、服务端读
    `qsize()` 之前就把消息取走，那一刻 `queued=0` 且 `busy` 还没置位，只按公式算得 0，
    于是"一条都不等"就返回，回复被丢掉。

    该公式假设**本客户端是唯一发送方**（首期单用户）。多客户端同时投递时会算少，表现为
    提前收工——不猜也不假装，见模块 docstring 的取舍说明。
    """
    return max(1, (1 if ack.get("busy") else 0) + int(ack.get("queued") or 0))


async def _drain_turn(console: Console, client: HostClient, expected: int) -> bool:
    """渲染事件直到本回合结束。返回 `False` 表示连接断了。

    `expected` 是"还要看到几个回合结束帧"，由 `_expected_turn_ends` 算出。之所以不是
    简单地"看到第一个结束帧就收工"：连上时如果 host 正在跑别人的回合，第一个结束帧属于
    那个回合，提前收工会把我们的回复丢在后面没人渲染。
    """
    from tui.app import _spinner, _StreamRenderer

    spinner = asyncio.create_task(_spinner(sys.stdout))
    renderer = _StreamRenderer(console, spinner)
    try:
        seen = 0
        while seen < expected:
            frame = await client.next_event()
            if frame is None:
                console.print("[red]连接已断开（host 可能已退出）[/]")
                return False
            if _render_frame(
                renderer, console, str(frame.get("event")), frame.get("data") or {}
            ):
                seen += 1
        if not renderer.has_started:
            console.print("[dim]No response[/]")
        return True
    finally:
        if not spinner.done():
            spinner.cancel()
        sys.stdout.write("\n")
        sys.stdout.flush()


async def _client_loop(
    console: Console, client: HostClient, *, prompt_source: Any = None
) -> int:
    """交互式客户端循环：读输入 → 投给 host → 渲染到该回合结束。"""
    session = prompt_source or PromptSession(history=InMemoryHistory())

    while True:
        try:
            text = await session.prompt_async(ANSI("\033[2mhost>\033[0m "))
            text = (text or "").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Bye![/]")
            return 0

        if not text:
            continue
        if text in _QUIT_COMMANDS:
            console.print("[yellow]Bye![/]")
            return 0
        if text.startswith("/"):
            if text == "/help":
                console.print(
                    "[dim]可用：/quit 退出（host 继续跑）、/status 看当前 run、"
                    "/help。其余斜杠命令需要本地会话，host 模式下不可用。[/]"
                )
            elif text == "/status":
                await _print_status(console, client)
            else:
                console.print(
                    f"[yellow]{text} 在 host 模式下不可用[/]"
                    "[dim]（需要本地会话；要完整功能请把 features.host.enabled 设为 false）[/]"
                )
            continue

        console.print(f"[bold]You:[/] {text}")
        console.print()

        try:
            ack = await client.send_message(text)
        except (ConnectionError, HostError) as e:
            console.print(f"[red]投递失败：{e}[/]")
            return 1

        # 还要等几个回合结束帧（正在跑的那个 + 排队中的，含本条）
        expected = _expected_turn_ends(ack)
        if ack.get("busy"):
            console.print("[dim]host 正在处理上一个回合，你的输入已排队[/]")

        try:
            if not await _drain_turn(console, client, expected):
                return 1
        except KeyboardInterrupt:
            # 客户端断开**不影响** host 继续跑这个回合——这正是本特性的核心承诺，
            # 所以 Ctrl-C 的语义是"我不看了"，不是"停下这个任务"（协议里没有取消
            # 命令，首期也不打算加）。
            console.print(
                "\n[yellow]已停止显示（host 仍在跑这个回合；重新运行即可继续查看）[/]"
            )
            return 130


async def _print_status(console: Console, client: HostClient) -> None:
    """打印当前活跃 run 的状态（客户端唯一能直接问到的信息）。"""
    try:
        run = await client.get_run()
    except (ConnectionError, HostError) as e:
        console.print(f"[red]查询失败：{e}[/]")
        return
    console.print(
        f"[dim]run {run.get('id')} / {run.get('status')}"
        f"（busy={run.get('busy')}，pid={run.get('pid')}）[/]"
    )


async def run_host_mode(
    *,
    providers: list[Any],
    policy: str = "deny_all",
    port: int = 0,
    prompt_source: Any = None,
    workspace: str | Path | None = None,
) -> int:
    """host 客户端模式的入口。返回进程退出码。

    `prompt_source` 可注入（任何有 `async prompt_async(...)` 的对象），测试靠它
    脚本化输入，不必起真终端。
    """
    console = Console()
    ws = Path(workspace) if workspace else Path.cwd()

    client: HostClient | None = None
    embedded: Any | None = None
    try:
        client, embedded = await _connect_or_embed(
            console, ws, providers=providers, policy=policy, port=port
        )
        _print_banner(console, client, embedded=embedded is not None)
        return await _client_loop(console, client, prompt_source=prompt_source)
    finally:
        if client is not None:
            client.close()
        if embedded is not None:
            # 内嵌 host 必须显式关掉：留着就是一个持着锁、状态永远 `running` 的 run。
            # `request_shutdown()` + `wait_closed()` 会把它落成 `completed` 并放锁。
            embedded.request_shutdown()
            await embedded.wait_closed()
