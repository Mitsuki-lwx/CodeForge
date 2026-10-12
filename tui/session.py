"""会话编排层 —— 把一次输入跑成一轮 Agent 会话。

从 `tui/app.py` 抽出：这一层负责装配后的**运行**（`run` / `run_async`）、
事件消费（`consume_agent_stream`）、ask 与计划的人工应答（`handle_hitl` /
`handle_plan_ready`）、后台任务通知（`consume_task_done`）与状态行刷新。

`CodeForgeApp`（`tui/app.py`）是状态容器；本模块只**读/驱动**它。
两者互相调用一次（`CodeForgeApp.inject_and_run` → `inject_and_run`），
那一处用函数内延迟 import 打破循环，见 `tui/app.py`。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console

if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError
else:
    # `prompt_toolkit/output/win32.py` 顶部就写着 `assert sys.platform == "win32"`，
    # 在 Linux/macOS 上 import 它会直接 AssertionError（实测把 4 个测试文件的收集
    # 全炸了）。这里给个同名占位：这个 `except` 子句在非 Windows 上永远不匹配，
    # 但代码保持可移植，不必到处写平台判断。
    class NoConsoleScreenBufferError(Exception):
        """非 Windows 上不存在；占位以便 `except` 子句可移植。"""


from config.loader import load_config, load_host_config
from core.agent.bootstrap import (
    CommandRegistrationError,
    SessionBundle,
    build_session,
)
from core.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    CompactEvent,
    HITLRequired,
    PlanReady,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from core.archive import (
    cleanup_expired,
)
from core.permissions.hitl import HITLChoice
from core.permissions.modes import PermissionMode
from core.permissions.rules import extract_content
from core.permissions.upgrade import ApprovalUpgrader
from tui import agent_view
from tui.app import CodeForgeApp
from tui.completer import CommandCompleter
from tui.hitl_dialog import show_hitl_dialog
from tui.provider_select import select_provider
from tui.render import (
    _LANG_HINTS,
    _command_key_bindings,
    _detect_language,
    _HeadlessSessionStub,
    _print_banner,
    _progress_hint,
    _render_compact_event,
    _spinner,
    _StreamRenderer,
)
from tui.select import read_line, select_from_options
from tui.teammate_status import build_status_text


def _print_no_console_help() -> None:
    """非 TTY 下的可操作提示（而不是一屏 prompt_toolkit 栈）。"""
    print(
        "无法启动交互界面：当前环境没有可用的控制台（stdin/stdout 不是终端）。\n"
        "\n"
        "  在真正的终端窗口里运行（cmd / PowerShell / Windows Terminal），或者：\n"
        '    codeforge --task "<任务>"       # 无头跑一次，不需要控制台\n'
        "\n"
        "（管道 / 重定向 / 部分 IDE 内置终端 / CI 都会走到这里。）",
        file=sys.stderr,
    )


async def _handle_plan_ready(
    app: CodeForgeApp,
    event: PlanReady,
) -> None:
    """Plan Mode 审批流程。"""
    from core.agent.plan_mode import build_plan_mode_exit_reminder

    console = app.console
    agent = app.agent

    # 显示 plan 内容
    plan_path = event.plan_path
    plan_content = event.plan_content

    console.print()
    console.print(f"[bold yellow]Plan Ready[/] [dim]→ {plan_path}[/]")
    if plan_content:
        preview = plan_content[:500] + ("..." if len(plan_content) > 500 else "")
        console.print(f"[dim]{preview}[/]")
    console.print()

    index = select_from_options(
        console,
        title="Approve plan?",
        subtitle=plan_path,
        options=[
            "Yes — exit plan mode & execute",
            "Give feedback to revise",
            "No / stay in plan mode",
        ],
        hint="↑/↓ 选择，Enter 确认，Esc 保持 Plan Mode",
        cancel_index=2,
    )

    plan_exists = bool(plan_path and Path(plan_path).exists())
    exit_msg = build_plan_mode_exit_reminder(plan_path, plan_exists)

    if index == 0:
        # 退出 Plan Mode → 执行
        agent.set_permission_mode(PermissionMode.DEFAULT)
        console.print("[green]Plan approved! Executing...[/]")
        execute_text = (
            exit_msg + "\n\nUser has approved your plan. You can now start coding."
        )
        if plan_content:
            execute_text += "\n\nApproved Plan:\n" + plan_content
        await _inject_and_run(app, execute_text)

    elif index == 1:
        # 给反馈 → 仍在 Plan Mode
        feedback = read_line(console, "Feedback > ")
        if feedback.strip():
            console.print("[yellow]Sending feedback...[/]")
            await _inject_and_run(app, feedback.strip())
        else:
            console.print("[dim]No feedback. Staying in plan mode.[/]")

    else:
        # No / Esc → 保持 Plan Mode
        console.print("[dim]Staying in plan mode. Type /plan to toggle off.[/]")


async def _inject_and_run(
    app: CodeForgeApp,
    text: str,
) -> None:
    """注入一条用户消息并运行 Agent 循环。"""
    console = app.console
    agent = app.agent
    sys.stdout.flush()

    # 语言检测：根据用户输入动态匹配回复语言
    lang = _detect_language(text)
    if lang and lang in _LANG_HINTS:
        text = text + _LANG_HINTS[lang]

    spinner_task = asyncio.create_task(_spinner(sys.stdout))
    renderer = _StreamRenderer(console, spinner_task)
    app.agent_running = True

    error_occurred, _ = await _consume_agent_stream(
        app, agent.run(text), renderer, console
    )

    if not spinner_task.done():
        spinner_task.cancel()
    app.agent_running = False

    if not error_occurred and renderer.has_started:
        sys.stdout.write("\n")
        sys.stdout.flush()
    console.print("[dim]---[/]\n")


async def _consume_agent_stream(
    app: CodeForgeApp,
    event_gen: AsyncGenerator[AgentEvent, None],
    renderer: _StreamRenderer,
    console: Console,
    *,
    track_text: bool = False,
    stall_heartbeat: float = 30.0,
    stall_warn_at: float = 60.0,
) -> tuple[bool, str]:
    """统一消费 Agent 事件流:分派渲染 + 卡死看门狗 + 报错原因为先。

    解决两类问题:
      A. 卡死但无感知 —— 若某段事件流静默超过 STALL_HEARTBEAT,打印已等待时长,
         让"思考中/无输出"随时有可视进展,不再看似死机。
      B. 卡住但没显示原因 —— 用通用 except 捕获客户端侧的任何异常,以红色
         "[原因]" 明示;若走了 KeyboardInterrupt,打印 Cancelled。

    Returns:
        (error_occurred, full_text)
    """
    full_text = ""
    error_result = False
    # 事件统一经队列从生产者流转到本循环:看门狗只等队列,不触发生成器,
    # 因此超时不取消生成器(直接超时在 __anext__ 上会 CancelledError 杀掉 agent.run)。
    events: asyncio.Queue = asyncio.Queue()
    producer_task = asyncio.create_task(_run_producer(event_gen, events))

    # ── 卡死看门狗:事件流静默超阈值 → 明示"仍在等待"与已等待时长 ──
    last_event_at = time.monotonic()  # 只在真实事件到达时重置 → "已静默 Xs" 真实
    last_heartbeat_at = time.monotonic()
    start = time.monotonic()

    try:
        while True:
            try:
                event = await asyncio.wait_for(events.get(), timeout=stall_heartbeat)
            except TimeoutError:
                now = time.monotonic()
                if now - last_heartbeat_at >= stall_heartbeat:
                    waited = now - last_event_at
                    total = now - start
                    # 说清在等谁：跑子 Agent 期间等待的不是模型
                    hint = _progress_hint(app)
                    console.print(
                        f"[dim]… 仍在等待{hint} (已静默 {waited:.0f}s, "
                        f"累计 {total:.0f}s)…[/]"
                    )
                    if waited >= stall_warn_at:
                        console.print(
                            f"[yellow]⚠ {hint} 长期无进展 ({waited:.0f}s)。"
                            f"可能网络中断 / 上游卡住 / 子 Agent 卡在某个工具上。"
                            f"Ctrl-C 取消。[/]"
                        )
                    last_heartbeat_at = now
                continue
            if event is None:  # 生产者 sentinel:流结束
                break

            # 生产者捕获到的 agent/client 异常 → 明失踪原因
            if isinstance(event, BaseException):
                if isinstance(event, (KeyboardInterrupt, asyncio.CancelledError)):
                    raise event
                renderer._stop_spinner()
                console.print(f"[red]x agent/client error: {event!r}[/]")
                app.agent_running = False
                error_result = True
                break

            last_event_at = time.monotonic()
            if isinstance(event, ThinkingDelta):
                renderer.on_thinking(event.text)
            elif isinstance(event, TextDelta):
                renderer.on_text(event.text)
                if track_text:
                    full_text += event.text
            elif isinstance(event, ToolCallStarted):
                renderer.on_tool_start(event.name, event.input)
            elif isinstance(event, ToolCallFinished):
                renderer.on_tool_finish(
                    event.name,
                    event.input,
                    event.success,
                    event.result_preview,
                    event.duration_ms,
                )
            elif isinstance(event, CompactEvent):
                renderer._stop_spinner()
                _render_compact_event(console, event)
            # IterationUpdate 仅作进度计数,UI 不展示具体轮次
            elif isinstance(event, AgentFinished):
                usage = event.total_usage or {}
                sys.stdout.write(
                    f"\n\033[2m({event.elapsed_s:.1f}s, {event.iterations}r, "
                    f"in:{usage.get('input_tokens', 0)} out:{usage.get('output_tokens', 0)}t)\033[0m"
                )
                sys.stdout.flush()
            elif isinstance(event, AgentError):
                renderer._stop_spinner()
                # 报错原因明示:message(code),不再只留一个憋着的"思考中"
                console.print(f"[red]x {event.message} ({event.code})[/]")
                app.agent_running = False
                error_result = True
                break
            elif isinstance(event, HITLRequired):
                renderer._stop_spinner()
                await _handle_hitl(app, event)
            elif isinstance(event, PlanReady):
                renderer._stop_spinner()
                app.agent_running = False
                await _handle_plan_ready(app, event)
    except KeyboardInterrupt:
        app.agent.cancel()
        renderer._stop_spinner()
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        console.print("[yellow]Cancelled (Ctrl-C).[/]")
        app.agent_running = False
        error_result = True
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 —— 客户端侧任何异常都明失踪原因
        renderer._stop_spinner()
        console.print(f"[red]x unexpected agent/client error: {e!r}[/]")
        app.agent_running = False
        error_result = True
    finally:
        if not producer_task.done():
            producer_task.cancel()

    return error_result, full_text


async def _run_producer(event_gen, events: asyncio.Queue) -> None:
    """消费 agent 事件流并塞进队列;异常对象入队供消费者显示,末尾 None 哨兵收尾。"""
    try:
        async for event in event_gen:
            await events.put(event)
    except BaseException as e:  # 把异常传给消费者以便显示原因(而非静默吞掉)
        try:
            await events.put(e)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await events.put(None)
        except Exception:  # noqa: BLE001 —— 队列已关闭
            pass


async def _handle_hitl(
    app: CodeForgeApp,
    event: HITLRequired,
) -> None:
    """处理 HITL 权限确认。"""
    from core.permissions.hitl import HITLRequest

    console = app.console
    agent = app.agent

    request = HITLRequest(
        tool_name=event.tool_name,
        description=event.description,
        arguments=event.arguments,
        risk_hint=event.risk_hint,
        origin=event.origin,
    )
    response = show_hitl_dialog(console, request)

    allowed = response.choice != HITLChoice.DENY
    if response.choice == HITLChoice.ALLOW_SESSION and not event.origin:
        # 只为主 Agent 自己的请求记账本。子 Agent 的「本会话允许」由它自己
        # 记在**自己的** checker 上（见 `Agent._execute_tools` 的升级分支）——
        # 记到主 Agent 这里会让"为子 Agent 放行"变成"给主 Agent 也放行"。
        content = extract_content(event.tool_name, event.arguments)
        agent._permission_checker.add_session_allow(event.tool_name, content)

    if response.choice == HITLChoice.ALLOW_SAVE and not event.origin:
        # 「允许并保存」= 落盘到项目级 `.codeforge/settings.json` 的 rules。
        # 旧实现把它当成 ALLOW_SESSION（只进内存），用户以为记下了、重启就没了。
        content = extract_content(event.tool_name, event.arguments)
        saved = agent._permission_checker.add_project_rule(
            app.workspace, event.tool_name, content, "allow"
        )
        console.print(
            f"[dim]已{'保存' if saved else '尝试保存'}规则："
            f"{event.tool_name} → {content}（{'项目级' if saved else '保存失败，本次仍放行'}）[/]"
        )

    agent.resolve_hitl(event.tool_use_id, allowed, response.choice.value)


def run(
    task: str = "",
    loop: str = "",
    host: bool | None = None,
    unattended_policy: str | None = None,
) -> None:
    """启动 TUI；`host` 打开时改为 host 客户端模式。

    `host` 为 `None` 表示"没显式指定"→ 取 `features.host.enabled`（默认关）。
    `unattended_policy` 为 `None` 表示"没显式指定"→ 无头（`--task`）取 `deny_all`。
    """
    providers = load_config("config.yaml")

    # 默认路径（host 关）行为与引入本开关之前完全一致：下面只多读一次配置，
    # 不影响 provider 选择、装配与交互循环的任何一步。
    host_cfg = load_host_config("config.yaml")
    requested = host_cfg.enabled if host is None else host

    if requested and not task:
        # 客户端模式下 TUI 不装配会话——装配了就会和 host 抢同一个 agent 与单写者锁。
        # 因此这里也**不做**交互式 provider 选择：provider 只在内嵌回落时才用得上，
        # 而 host 本来就是无人值守入口，挑 provider 不该发生在连上之前。
        from tui.host_mode import run_host_mode

        try:
            asyncio.run(
                run_host_mode(
                    providers=providers,
                    policy=host_cfg.unattended_policy,
                    port=host_cfg.port,
                )
            )
        except KeyboardInterrupt:
            print("\nBye!")
        return

    if requested:
        # 无头单任务没有"要保住的会话"，而把它架在 loopback 上会让每条 E2E / 基准
        # 测试都依赖一个监听套接字。明确说一句，别让 `--host` 看起来生效了其实没有。
        print("提示：host 模式不适用于 --task（无头单任务），本次走原路径。")

    # 无头单任务模式：跳过交互式方向键选择，取第一个配置的 provider
    provider = providers[0] if task else select_provider(providers)
    try:
        asyncio.run(
            _run_async(
                provider,
                task=task,
                providers=providers,
                loop=loop,
                unattended_policy=unattended_policy,
            )
        )
    except KeyboardInterrupt:
        print("\nBye!")


async def _run_async(
    provider,
    task: str = "",
    providers=None,
    loop: str = "",
    unattended_policy: str | None = None,
) -> None:
    console = Console()
    workspace = Path.cwd()

    # 崩溃恢复提示：启动时若有最近会话（进程异常退出后 JSONL 仍在），提示可 /resume 继续。
    # 仅交互模式；无头(--task)不打扰。
    if not task:
        try:
            from core.archive import list_sessions

            items = list_sessions(workspace)
            if items:
                recent = items[0]
                console.print(
                    f"[dim]最近会话: {recent.title} ({recent.relative_time}) — "
                    f"/resume 可恢复[/]"
                )
        except Exception:  # noqa: BLE001 —— 提示失败不阻断启动
            pass

    # 后台会话清理，不阻塞启动（F26）
    cleanup_task = asyncio.create_task(
        asyncio.to_thread(cleanup_expired, str(workspace))
    )

    bundle: SessionBundle | None = None
    app: CodeForgeApp | None = None
    _task_done_consumer: asyncio.Task | None = None
    _status_ticker: asyncio.Task | None = None
    # 审批升级通道：只有**交互式 TUI** 才建。无头单任务（`--task`）保持既有语义
    # （ask 由无人值守策略代答），不能因为多了个通道就改变它的行为。
    _approval_upgrader: ApprovalUpgrader | None = None
    if not task:
        _approval_upgrader = ApprovalUpgrader()

    try:
        # ── 唯一装配入口：新建与恢复走同一条路径（core/agent/bootstrap.py）──
        # client / registry / hooks / runtime / writer / agent / skills /
        # 会话状态 / team / loop / coordinator / ExitPlanMode 回调 / MCP
        # 全部收敛在 build_session 内，避免两条路径的能力集分叉。
        try:
            bundle = await build_session(
                provider=provider,
                workspace=workspace,
                loop_spec=loop,
                headless=bool(task),
                unattended_policy=unattended_policy,
                approval_upgrader=_approval_upgrader,
            )
        except CommandRegistrationError as e:
            print(f"命令注册冲突，启动终止: {e}")
            sys.exit(1)

        for _notice in bundle.notices:
            console.print(f"[dim]{_notice}[/]")

        _print_banner(console, provider.name, provider.model)

        # 无头模式必须**说清楚**当前授权档位：旧实现静默全放行，
        # 用户以为 agent 在"只读地看一眼"，实际它能跑任意命令。
        if task:
            _eff = (
                bundle.agent.unattended_policy.value
                if getattr(bundle.agent, "unattended_policy", None) is not None
                else "none"
            )
            console.print(
                f"[yellow]无头模式：无人值守策略 = {_eff}[/]"
                + (
                    "（只读；要允许改文件/执行命令，加 "
                    "--unattended-policy allow_write / review / allow_all）"
                    if _eff == "deny_all"
                    else ""
                )
            )

        # ── 输入历史 + 命令补全 ──
        # 无头单任务模式不进入交互循环：跳过 PromptSession（GUI-less shell 会因
        # 无 Windows 屏幕缓冲区抛 NoConsoleScreenBufferError），用最小占位。
        history = InMemoryHistory()
        if task:
            session = _HeadlessSessionStub()
        else:
            try:
                session = PromptSession(
                    history=history,
                    completer=CommandCompleter(bundle.cmd_registry),
                    complete_while_typing=True,
                    key_bindings=_command_key_bindings(),
                )
            except NoConsoleScreenBufferError:
                # ⚠️ 非 TTY（管道 / 重定向 / 某些 IDE 终端 / CI）下 prompt_toolkit
                # 造不出输出层。**这里必须兜住**：以前它直接冒到顶层，
                # 用户看到的是一屏 `NoConsoleScreenBufferError` 栈，
                # 而真正该说的是"换个终端，或者用无头模式"。
                _print_no_console_help()
                bundle.close()
                raise SystemExit(1) from None

        app = CodeForgeApp(
            console=console,
            agent=bundle.agent,
            session=session,
            conversation=bundle.conversation,
            runtime=bundle.runtime,
            provider=provider,
            mcp_pool=bundle.mcp_pool,
            workspace=str(workspace),
            notes=bundle.notes,
            writer=bundle.writer,
            cmd_registry=bundle.cmd_registry,
            skill_loader=bundle.skill_loader,
            skill_executor=bundle.skill_executor,
            task_mgr=bundle.task_mgr,
            subagent_catalog=bundle.subagent_catalog,
            wt_manager=bundle.wt_manager,
            team_mgr=bundle.team_mgr,
            state=bundle.state_store,
            provider_list=providers or [],
        )
        app.hook_runner = bundle.hook_runner

        # 启动 task notification 消费协程
        _task_done_consumer = asyncio.create_task(_consume_task_done(app))

        # 审批升级通道：起独立消费者 Task。**必须独立**——主协程在 `await` 子 Agent
        # 时是卡住的，自己取不了请求（实测那会死锁）；消费者若同步弹窗又会冻住整个
        # 事件循环（连后台子 Agent 一起冻）。形态选择的实测对比见 upgrade 模块 docstring。
        app.approval_upgrader = _approval_upgrader
        if _approval_upgrader is not None:
            _approval_upgrader.start(lambda req: show_hitl_dialog(app.console, req))

        # ── Hook: 会话启动（首条用户消息进入对话前）──
        await bundle.hook_runner.run(
            "session_start", app._hook_payload("session_start")
        )

        # ── 单任务(无头)模式：跑一次后由 finally 走清理退出 ──
        if task:
            await _inject_and_run(app, task)
            # 等后台子 agent 完成 + Lead 空闲稳定后才退出，避免进程提前退出中断它们
            # （后台完成会经 _consume_task_done 主动唤醒 Lead 继续处理）。
            await _wait_background_tasks(app)
            return

        # ── 模型路由（spec_router）：默认关，features.router.enabled 显式开启 ──
        from core.agent.router import (
            judge_and_route as _judge_route,
        )
        from core.agent.router import (
            resolve_router as _resolve_router,
        )

        _router_cfg = (
            getattr(bundle.team_features, "router", None)
            if bundle.team_features
            else None
        )
        app.router_cfg = _router_cfg  # 暴露给 /model：切到 cheap 时提示路由停用
        _router_enabled = bool(_router_cfg and _router_cfg.enabled)
        _router_prompt = getattr(_router_cfg, "judge_prompt", "") if _router_cfg else ""
        _router_cheap = (
            getattr(_router_cfg, "cheap_tier", "cheap") if _router_cfg else "cheap"
        )

        # ── 后台任务 / 队友状态行刷新（只读可见性，见 spec_teammate_status）──
        _status_ticker = asyncio.create_task(_refresh_status_bar(app))

        while True:
            # ── 输入 ──
            try:
                hint = (
                    f"[{app.agent.mode.value}] "
                    if app.agent.mode.value != "off"
                    else ""
                )
                text = await session.prompt_async(
                    ANSI(f"\033[2m{hint}>\033[0m "),
                    # 状态行：后台任务 / 队友跑到哪了。返回 None 时不占行
                    # （无运行中任务 = 与引入本功能前逐字一致）。
                    bottom_toolbar=lambda: build_status_text(app.task_mgr),
                )
                text = text.strip()
            except (EOFError, KeyboardInterrupt):
                if app.agent_running:
                    app.agent.cancel()
                    app.agent_running = False
                    console.print("\n[yellow]Cancelled. Session still active.[/]")
                    continue
                console.print("\n[yellow]Bye![/]")
                return

            if not text:
                continue

            # ── 命令分流：斜杠走本地分发，非命令送 Agent ──
            if await app.dispatch_slash(text):
                console.print("[dim]---[/]\n")
                continue

            console.print(f"[bold]You:[/] {text}")
            console.print()
            sys.stdout.flush()

            # 语言检测：根据用户输入动态匹配回复语言
            lang = _detect_language(text)
            if lang and lang in _LANG_HINTS:
                text = text + _LANG_HINTS[lang]

            # ── 模型路由：入口复杂度判断（spec_router）──
            # features.router.enabled=True 才启用；simple 走便宜直接答，complex 转主。
            # 未启用/失败一律 continue 到主循环（不丢消息）。
            _router = _resolve_router(
                app.provider_list,
                app.provider,
                enabled=_router_enabled,
                cheap_tier=_router_cheap,
            )
            if _router is not None:
                _cheap, _ = _router
                _kind, _answer = await _judge_route(
                    _cheap, text, judge_prompt=_router_prompt or None
                )
                if _kind == "simple" and _answer:
                    console.print(
                        f"[cyan]➤ {getattr(_cheap, 'name', '便宜')}[/] {_answer}"
                    )
                    console.print("[dim]---[/]\n")
                    continue

            # ── 启动加载动画（首个 chunk 到达前）──
            spinner_task = asyncio.create_task(_spinner(sys.stdout))
            renderer = _StreamRenderer(console, spinner_task)

            # ── Agent 循环（统一消费:分派 + 看门狗 + 报错原因）──
            app.agent_running = True
            start_time = time.monotonic()

            error_occurred, full_text = await _consume_agent_stream(
                app, app.agent.run(text), renderer, console, track_text=True
            )

            if not spinner_task.done():
                spinner_task.cancel()
            app.agent_running = False

            # ── 分隔与收尾 ──
            if not error_occurred and renderer.has_started:
                sys.stdout.write("\n")
                sys.stdout.flush()

            if not renderer.has_started and not error_occurred:
                console.print(
                    f"[dim]No response ({time.monotonic() - start_time:.1f}s)[/]"
                )

            console.print("[dim]---[/]\n")

    finally:
        # ── 停止审批消费者 ──
        if _approval_upgrader is not None:
            await _approval_upgrader.stop()
        # ── 停止 task notification 消费 ──
        if _task_done_consumer is not None and not _task_done_consumer.done():
            _task_done_consumer.cancel()
        # ── 停止状态行刷新 ──
        if _status_ticker is not None and not _status_ticker.done():
            _status_ticker.cancel()
        # ── 取消所有后台子 Agent ──
        cur_task_mgr = app.task_mgr if app is not None else None
        if cur_task_mgr is not None:
            try:
                await cur_task_mgr.cancel_all()
            except Exception:  # noqa: BLE001, S110 —— 退出兜底，失败可忽略
                pass
        # ── 清理可自动清理的过期 worktree（不碰用户创建的）──
        cur_wt = app.wt_manager if app is not None else None
        if cur_wt is not None:
            try:
                await cur_wt.cleanup_all_generated()
            except Exception:  # noqa: BLE001, S110 —— 清理失败仅告警
                pass
        # ── Hook: 会话结束（进程退出兜底，覆盖 quit 与异常退出）──
        if app is not None and app.hook_runner is not None:
            try:
                await app.hook_runner.run(
                    "session_end", app._hook_payload("session_end")
                )
            except Exception:  # noqa: BLE001, S110 —— hook 失败不影响退出
                pass
        # 等待后台记忆任务 + 关闭存档 + 清理
        # app 未构造时（装配中途失败）退回 bundle 的产物，保证句柄不泄漏
        cur_agent = (
            app.agent
            if app is not None
            else (bundle.agent if bundle is not None else None)
        )
        cur_writer = (
            app.writer
            if app is not None
            else (bundle.writer if bundle is not None else None)
        )
        if cur_agent is not None:
            await cur_agent.shutdown_memory(timeout=3.0)
        if cur_writer is not None:
            cur_writer.close()
        if not cleanup_task.done():
            cleanup_task.cancel()
        if bundle is not None:
            try:
                await bundle.mcp_pool.close_all()
            except Exception:  # noqa: BLE001, S110 —— 关闭失败不影响退出
                pass


async def _wait_background_tasks(
    app: CodeForgeApp,
    *,
    idle_seconds: float = 1.0,
    timeout: float = 300.0,
) -> None:
    """无头单任务模式：等后台子 agent 全部完成 + Lead 空闲稳定后才返回。

    后台任务完成时会经 _consume_task_done 主动唤醒 Lead 跑新一轮，所以这里要等
    「无 RUNNING 任务 且 Lead 未在跑」持续 idle_seconds 才返回；timeout 兜底防死等。
    """
    from core.task.manager import TaskStatus

    if app.task_mgr is None:
        return
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        running = any(t.status == TaskStatus.RUNNING for t in app.task_mgr.list())
        busy = running or app.agent_running
        if not busy:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= idle_seconds:
                return
        else:
            stable_since = None
        await asyncio.sleep(0.2)


async def _consume_task_done(app: CodeForgeApp) -> None:
    """后台任务完成通知消费协程。

    从 BackgroundTaskManager 的 done 队列中取 task_id，
    查询任务状态，将 <task-notification> 注入 runtime.pending_reminders。
    主 Agent 下一次 run 时自然消费。
    """
    if app.task_mgr is None:
        return

    q = app.task_mgr.subscribe_done()
    while True:
        try:
            task_id = await q.get()
        except RuntimeError:
            return  # 事件循环已关闭

        bt = app.task_mgr.get(task_id)
        if bt is None:
            continue

        status_str = {0: "running", 1: "completed", 2: "failed", 3: "cancelled"}.get(
            int(getattr(bt, "status", 0)), "unknown"
        )
        result = getattr(bt, "result", "") or "[no result]"
        name = getattr(bt, "name", "") or "unnamed"

        notification = (
            f"<task-notification>\n"
            f'Task {task_id} (name="{name}"): {status_str}\n'
            f"Result: {result}\n"
            f"</task-notification>"
        )

        # ── 人排队的消息：任务落下就替他说出去（spec_teammate_inspect §3.4）──
        # 顺序要紧：**先**按第一轮结果组装通知，**再**投递续派 —— 续派会把 result
        # 清空、状态置回 RUNNING；反过来先投递，通知里就没结果了（变异注入验过）。
        note = await agent_view.deliver_pending(app, task_id)
        if note:
            notification += f"\n{note}"
            app.console.print(
                f"[red]{note}[/]" if note.startswith("x ") else f"[dim]{note}[/]"
            )

        # 注入到 runtime pending_reminders（供主 Agent 下一次 run 消费）
        if app.runtime is not None:
            try:
                app.runtime.pending_reminders.append(notification)
            except AttributeError:
                # runtime 没有 pending_reminders 属性，直接注入到会话
                app.conversation.add_system_reminder(notification)

        # 非阻塞通知到 console
        if status_str == "failed":
            app.console.print(f"[yellow]⚠ Task {task_id} failed[/]")

        # ── 结果主动投递给活着的 Lead（对齐参考 mewcode）──
        # 完成通知立即作为下一轮注入；若 agent 未被占用则主动唤醒跑一轮，避免 Lead
        # 卡在 sleep+ls 轮询里等结果。无需额外空 user 轮，通知本身即本轮输入。
        if not app.agent_running:
            try:
                await _inject_and_run(app, notification)
            except Exception as e:  # noqa: BLE001 —— 唤醒失败仅告警，不阻塞通知循环
                app.console.print(f"[dim]task-done wake failed: {e}[/]")


# ── 状态行刷新（后台任务 / 队友可见性）─────────────────────────────

# 检查节奏（秒）。只做"取字段 + 拼字符串"的纯计算，无 IO。
_STATUS_TICK = 0.5


async def _refresh_status_bar(app: CodeForgeApp) -> None:
    """定期让提示符重绘，使 `bottom_toolbar` 反映最新任务状态。

    只在**渲染文本发生变化**时 invalidate：

    - 没有运行中的后台任务 → 文本恒为 `None`，首轮之后**不再触发任何重绘**（零开销）
    - 提示符未活跃（Agent 正在跑 / 无头模式）→ 跳过

    文本本身由 `tui.teammate_status.build_status_text`（纯函数）产出。
    动机见 `docs/spec_teammate_status.md`。
    """
    last: str | None = None
    while True:
        await asyncio.sleep(_STATUS_TICK)
        text = build_status_text(getattr(app, "task_mgr", None))
        if text == last:
            continue
        last = text
        prompt_app = getattr(getattr(app, "session", None), "app", None)
        if prompt_app is None or not getattr(prompt_app, "is_running", False):
            continue
        try:
            prompt_app.invalidate()
        except Exception:  # noqa: BLE001, S110 —— 提示符正要退出时忽略
            pass

        pass
