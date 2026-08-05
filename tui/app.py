"""CodeForge 终端对话 —— ch04 Agent Loop 版。

基于 core.agent.Agent 的事件流模型：
Agent 自主多轮调用工具直到任务完成，UI 只消费事件。"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console

from config.loader import load_config
from config.model import ProviderConfig
from config.protocol_defaults import effective_context_window
from conversation.manager import ConversationManager
from core.agent import Agent, AgentConfig
from core.agent.events import (
    AgentError,
    AgentFinished,
    CompactEvent,
    CompactPhase,
    HITLRequired,
    IterationUpdate,
    PlanReady,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from core.agent.runtime import SessionRuntime
from core.archive import Writer, cleanup_expired
from core.commands import Kind, Registry, parse_line, register_builtins
from core.commands.skill_register import register_skills_as_commands
from core.context_compression.state import new_session_context
from core.instructions import load_instructions
from core.mcp import ConnectionPool, MCPToolAdapter, load_mcp_config
from core.notes import NoteStore, build_memory_index_text
from core.permissions.hitl import HITLChoice
from core.permissions.modes import PermissionMode
from core.permissions.rules import extract_content
from core.skills import SkillExecutor, SkillLoader
from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from core.tool.tools.install_skill import InstallSkillTool
from core.tool.tools.load_skill import LoadSkillTool
from llm.client import LLMClient
from tui.completer import CommandCompleter
from tui.hitl_dialog import show_hitl_dialog
from tui.provider_select import select_provider
from tui.select import read_line, select_from_options

CAT_ASCII = r"""      /\_/\
     (o.o)
     > ^ <"""


def format_compact_notice(before: int, after: int) -> str:
    """压缩完成后的统一系统消息文案。"""
    return f"已压缩，token 从 {before} 降至 {after}"


def _command_key_bindings() -> KeyBindings:
    """回车行为：补全菜单打开且有高亮命令 → 直接执行；否则正常提交。"""

    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event):
        buf = event.current_buffer
        if buf.complete_state is not None and buf.complete_state.current_completion is not None:
            comp = buf.complete_state.current_completion
            buf.complete_state = None
            buf.apply_completion(comp)
            event.app.exit(result=buf.text)
        else:
            event.app.exit(result=buf.text)

    return kb


@dataclass
class CodeForgeApp:
    """TUI 应用状态容器，命令处理函数与事件循环共享。"""

    console: Console
    agent: Agent
    session: PromptSession
    conversation: ConversationManager
    runtime: SessionRuntime
    provider: ProviderConfig
    mcp_pool: ConnectionPool
    workspace: str = ""
    notes: object | None = None      # core.notes.NoteStore 实例
    writer: object | None = None     # core.archive.Writer 实例
    cmd_registry: object | None = None  # core.commands.Registry 实例
    agent_running: bool = False      # 主循环是否正在跑 Agent（idle 判定）
    skill_loader: object | None = None    # core.skills.SkillLoader 实例
    skill_executor: object | None = None  # core.skills.SkillExecutor 实例

    # ── UI Protocol 实现（handler 通过此接口操作 TUI）──────────────

    def println(self, msg: str) -> None:
        self.console.print(f"[dim]{msg}[/]")

    def error(self, msg: str) -> None:
        self.console.print(f"[red]x {msg}[/]")

    def mode(self) -> PermissionMode:
        return self.agent.permission_mode

    def set_mode(self, mode: PermissionMode) -> None:
        self.agent.set_permission_mode(mode)
        if mode == PermissionMode.PLAN and self.agent._plan_path is None:
            from core.agent.plan_mode import generate_plan_path

            self.agent._plan_path = generate_plan_path(str(self.workspace))
            self.agent._permission_checker.plan_file_path = str(
                self.agent._plan_path
            )

    async def inject_and_send(self, label: str, preset: str) -> None:
        """把预设提示词作为用户消息注入并触发 Agent 回合。"""
        self.console.print(f"[bold]{label}:[/] {preset}")
        self.conversation.add_user_message(preset)
        await _inject_and_run(self, preset)

    def usage_in(self) -> int:
        return self.agent._total_usage.get("input_tokens", 0)

    def usage_out(self) -> int:
        return self.agent._total_usage.get("output_tokens", 0)

    def model_name(self) -> str:
        return self.provider.model if self.provider else ""

    def cwd(self) -> str:
        return self.workspace

    def tool_count(self) -> int:
        try:
            return self.agent._registry.count()
        except Exception:
            return 0

    def memory_files(self) -> list[str]:
        if self.notes is None:
            return []
        project, user = self.notes.list_files()
        return project + user

    def session_path(self) -> str:
        return str(self.writer.path) if self.writer else ""

    def session_id(self) -> str:
        if self.runtime and self.runtime.session:
            return self.runtime.session.session_id
        return ""

    def quit(self) -> None:
        raise SystemExit(0)

    async def force_compact(self) -> None:
        if getattr(self.agent, "_runtime", None) is None:
            self.error("上下文压缩未启用（当前会话未绑定 runtime）。")
            return
        self.println("正在压缩上下文...")
        try:
            before, after = await self.agent.run_force_compact()
        except Exception as e:
            self.error(f"压缩失败: {e}")
            return
        self.println(format_compact_notice(before, after))

    async def open_resume_menu(self) -> None:
        """打开历史会话列表并恢复所选会话（上下键 + 回车）。"""
        from core.archive import list_sessions

        items = list_sessions(self.workspace)
        if not items:
            self.println("没有可恢复的历史会话。")
            return
        options = [
            f"{it.title}  [{it.relative_time}]  {it.model}" for it in items
        ]
        index = select_from_options(
            self.console,
            title="Resume a session",
            options=options,
            hint="↑/↓ 选择，Enter 恢复，Esc 取消",
            cancel_index=-1,
        )
        if index < 0:
            return
        sid = items[index].session_id
        self.println(f"正在恢复会话 {sid}...")
        try:
            msg = await self.resume_session(sid)
        except Exception as e:
            self.error(f"恢复失败: {e}")
            return
        self.println(msg)

    def clear_and_new_session(self) -> None:
        """结束当前会话并开启新会话（关闭旧 writer、重建对话与运行时）。"""
        if self.writer is not None:
            self.writer.close()
        try:
            new_ctx = new_session_context(self.workspace)
        except Exception as e:
            self.error(f"开启新会话失败: {e}")
            return
        new_writer = Writer(new_ctx.session_dir, model=self.provider.model)
        conversation = ConversationManager(
            system_prompt="",
            on_append=new_writer.append,
            on_replace=_make_on_replace(new_writer),
        )
        self.conversation = conversation
        self.writer = new_writer
        self.runtime.reset_for_new_session(new_ctx)
        self.agent._conversation = conversation
        self.agent._total_usage = {"input_tokens": 0, "output_tokens": 0}
        self.agent._runtime = self.runtime

    def idle(self) -> bool:
        return not self.agent_running

    # ── Skill 系统 UI 方法 ───────────────────────────────────────

    def list_catalog_skills(self) -> list[dict]:
        """列出 Catalog 中所有 Skill（name/description/source/mode）。"""
        if self.skill_loader is None:
            return []
        result = []
        for s in self.skill_loader.list_all():
            result.append({
                "name": s.meta.name,
                "description": s.meta.description,
                "source": self.skill_loader.get_source_label(s.meta.name),
                "mode": s.meta.mode,
            })
        return result

    def list_active_skills(self) -> list[str]:
        """列出当前已激活 Skill 的名字列表。"""
        if self.runtime is not None:
            return self.runtime.active_skills.names()
        return []

    def clear_active_skills(self) -> None:
        """清空当前已激活的全部 Skill。"""
        if self.agent is not None:
            self.agent.clear_active_skills()

    async def append_assistant_message(self, text: str) -> None:
        """向主对话追加一条 assistant 消息（fork 模式回流用）。

        add_assistant_message 会通过 on_append 回调自动写入会话存档，
        无需手动 writer.append。
        """
        self.conversation.add_assistant_message(text)

    # ── 命令分发 ───────────────────────────────────────────────────

    async def dispatch_slash(self, text: str) -> bool:
        """斜杠命令分发入口：是命令则本地处理并返回 True，否则返回 False。

        非 / 输入 → parse_line 返回 None → False（上层走 Agent 通道）。
        """
        call = parse_line(text)
        if call is None:
            return False
        if not call.name:
            self.println("未知命令：输入 /help 查看可用命令")
            return True
        cmd = self.cmd_registry.lookup(call.name)
        if cmd is None:
            self.println(f"未知命令: /{call.name}。输入 /help 查看可用命令")
            return True
        # 无 arg_hint 的命令不接受参数 → 按未命中处理
        if not cmd.arg_hint and call.args:
            self.println(f"命令 /{cmd.name} 不接受参数。输入 /help 查看用法")
            return True
        # UI / PROMPT 命令仅在 idle 可执行
        if cmd.kind in (Kind.UI, Kind.PROMPT) and not self.idle():
            self.error("请等待当前任务完成")
            return True
        try:
            await cmd.handler(self, call.args)
        except SystemExit:
            raise
        except Exception as e:
            self.error(str(e))
        return True

    async def inject_and_run(self, text: str) -> None:
        """注入一条用户消息并运行 Agent 循环（供 /do 等命令使用）。"""
        await _inject_and_run(self, text)

    async def resume_session(self, session_id: str) -> str:
        """恢复指定会话，重建 Conversation/Writer/Agent（F22）。

        Args:
            session_id: 会话 ID（YYYYMMDD-HHMMSS-xxxx）。

        Returns:
            恢复提示文本，如「已恢复会话 <id>，共 <N> 条消息」。
        """
        from core.agent import Agent, AgentConfig
        from core.agent.runtime import SessionRuntime
        from core.archive import restore_session
        from core.archive.writer import Writer
        from core.context_compression.state import SessionContext
        from core.tool.context import ExecutionContext
        from core.tool.tools import get_default_registry

        session_dir = (
            Path(self.workspace) / ".codeforge" / "sessions" / session_id
        )
        result = await restore_session(
            session_dir,
            provider_config=self.provider,
            context_window=self.runtime.context_window,
        )

        new_writer = Writer(session_dir, model=self.provider.model)
        result.conversation.set_callbacks(
            on_append=new_writer.append,
            on_replace=_make_on_replace(new_writer),
        )

        session_ctx = SessionContext(
            session_id=session_id,
            session_dir=str(session_dir),
            spill_dir=str(session_dir / "tool-results"),
        )
        new_runtime = SessionRuntime(
            session=session_ctx,
            context_window=self.runtime.context_window,
            notes=self.notes,
        )

        new_agent = Agent(
            registry=get_default_registry(),
            llm_client=LLMClient.create(self.provider),
            exec_ctx=ExecutionContext(cwd=Path(self.workspace), session_id=session_id),
            conversation=result.conversation,
            config=AgentConfig(max_iterations=25),
            runtime=new_runtime,
            instructions=load_instructions(self.workspace),
            memory=build_memory_index_text(self.notes),
        )

        # 关闭旧 writer，切换引用（旧会话 JSONL 保留不删，F24）
        if self.writer is not None:
            self.writer.close()
        self.agent = new_agent
        self.conversation = result.conversation
        self.runtime = new_runtime
        self.writer = new_writer
        return f"已恢复会话 {session_id}，共 {len(result.conversation.messages)} 条消息"


def _print_banner(console: Console, name: str, model: str) -> None:
    console.print(CAT_ASCII, style="cyan")
    console.print("[bold cyan]CodeForge[/] [white]0.1.0[/]")
    console.print(f"[dim]{name} - {model}[/]")
    console.print()


def _short_preview(content: str, max_len: int = 60) -> str:
    """结果/文本的单行短摘要（取首行、截断）。"""
    s = (content or "").strip()
    if not s:
        return ""
    first = s.splitlines()[0]
    if len(first) <= max_len:
        return first
    return first[: max_len - 3] + "..."


def _tool_title(name: str, input: dict) -> str:
    """工具调用的人类可读标题（参考 mewcode 的 _tool_title，避免原始 JSON 堆积）。"""
    n = name.lower()
    if n == "bash":
        cmd = input.get("command", "")
        short = cmd[:50] + "…" if len(cmd) > 50 else cmd
        return f"Bash: {short}" if short else "Bash"
    if n == "read_file":
        p = Path(input.get("file_path", "")).name if input.get("file_path") else ""
        return f"Read {p}" if p else "Read"
    if n == "write_file":
        p = Path(input.get("file_path", "")).name if input.get("file_path") else ""
        content = input.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"Write {p} ({lines} lines)" if p else "Write"
    if n == "edit_file":
        p = Path(input.get("file_path", "")).name if input.get("file_path") else ""
        return f"Edit {p}" if p else "Edit"
    if n == "glob":
        return f"Glob: {input.get('pattern', '')}"
    if n == "grep":
        return f"Grep: {input.get('pattern', '')}"
    if n == "exitplanmode":
        return "Submit plan"
    # MCP / 未知工具：挑一个主参做摘要
    for key in ("path", "file_path", "command", "pattern", "query", "url", "name"):
        val = input.get(key)
        if isinstance(val, str) and val:
            return f"{name}: {_short_preview(val, 50)}"
    return name


class _StreamRenderer:
    """流式输出渲染器：管理 spinner、思考块、回答分隔、工具步骤。

    思考内容以 dim 单独一块显示，正文前打「──── 回答 ────」分隔线，
    工具步骤用简洁标题（● / ✓ / ✗）呈现，三者清晰区分。
    """

    def __init__(self, console: Console, spinner_task) -> None:
        self.console = console
        self.spinner = spinner_task
        self.has_started = False   # 首个 Thinking/Text/Tool 到达，spinner 已停
        self.in_thinking = False   # 当前正处在思考块

    def _stop_spinner(self) -> None:
        if not self.has_started:
            self.has_started = True
            self.spinner.cancel()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def on_thinking(self, text: str) -> None:
        if not self.has_started:
            self.has_started = True
            self.spinner.cancel()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            sys.stdout.write("  \033[2m思考中\033[0m\n")
            sys.stdout.flush()
        if not self.in_thinking:
            self.in_thinking = True
        sys.stdout.write(f"\033[2m{text}\033[0m")
        sys.stdout.flush()

    def on_text(self, text: str) -> None:
        if not self.has_started:
            self.has_started = True
            self.spinner.cancel()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        if self.in_thinking:
            self.in_thinking = False
            sys.stdout.write("\n\033[2m──── 回答 ────\033[0m\n")
            sys.stdout.flush()
        sys.stdout.write(text)
        sys.stdout.flush()

    def on_tool_start(self, name: str, input: dict) -> None:
        self._stop_spinner()
        if self.in_thinking:
            self.in_thinking = False
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.console.print(f"  [yellow]●[/] {_tool_title(name, input)}")

    def on_tool_finish(self, name: str, input: dict, success: bool,
                      preview: str, duration_ms: int) -> None:
        icon = "[green]✓[/]" if success else "[red]✗[/]"
        dur = f" ({duration_ms / 1000:.1f}s)" if duration_ms else ""
        line = f"     {icon} {_tool_title(name, input)}{dur}"
        if not success:
            err = _short_preview(preview, 80)
            if err:
                line += f"  [dim]{err}[/]"
        self.console.print(line)


async def _handle_plan_ready(
    app: CodeForgeApp, event: PlanReady,
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
        execute_text = exit_msg + "\n\nUser has approved your plan. You can now start coding."
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
    app: CodeForgeApp, text: str,
) -> None:
    """注入一条用户消息并运行 Agent 循环。"""
    console = app.console
    agent = app.agent
    sys.stdout.flush()

    spinner_task = asyncio.create_task(_spinner(sys.stdout))
    renderer = _StreamRenderer(console, spinner_task)
    error_occurred = False

    try:
        async for event in agent.run(text):
            if isinstance(event, ThinkingDelta):
                renderer.on_thinking(event.text)

            elif isinstance(event, TextDelta):
                renderer.on_text(event.text)

            elif isinstance(event, CompactEvent):
                renderer._stop_spinner()
                _render_compact_event(console, event)

            elif isinstance(event, ToolCallStarted):
                renderer.on_tool_start(event.name, event.input)

            elif isinstance(event, ToolCallFinished):
                renderer.on_tool_finish(
                    event.name, event.input, event.success,
                    event.result_preview, event.duration_ms,
                )

            elif isinstance(event, AgentFinished):
                usage = event.total_usage
                sys.stdout.write(
                    f"\n\033[2m({event.elapsed_s:.1f}s, {event.iterations}r, "
                    f"in:{usage.get('input_tokens',0)} out:{usage.get('output_tokens',0)}t)\033[0m"
                )
                sys.stdout.flush()

            elif isinstance(event, AgentError):
                renderer._stop_spinner()
                console.print(f"[red]x {event.message} ({event.code})[/]")
                error_occurred = True

            elif isinstance(event, HITLRequired):
                renderer._stop_spinner()
                await _handle_hitl(app, event)

    except KeyboardInterrupt:
        agent.cancel()
        console.print("[yellow]Cancelled.[/]")
    finally:
        if not spinner_task.done():
            spinner_task.cancel()

    if not error_occurred and renderer.has_started:
        sys.stdout.write("\n")
        sys.stdout.flush()
    console.print("[dim]---[/]\n")


def _render_compact_event(
    console: Console, event: CompactEvent,
) -> None:
    """渲染上下文压缩状态事件到 scrollback。"""
    if event.phase == CompactPhase.BEFORE_AUTO:
        console.print("[dim]正在压缩上下文...[/]")
    elif event.phase == CompactPhase.BEFORE_EMERGENCY:
        console.print("[dim]上下文撞墙，自动压缩中...[/]")
    elif event.err is not None:
        console.print(f"[red]压缩失败: {event.err}[/]")
    else:
        console.print(f"[dim]{format_compact_notice(event.before, event.after)}[/]")




_SPINNER_INTERVAL = 0.2


async def _handle_hitl(
    app: CodeForgeApp, event: HITLRequired,
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
    )
    response = show_hitl_dialog(console, request)

    allowed = response.choice != HITLChoice.DENY
    if response.choice == HITLChoice.ALLOW_SESSION:
        content = extract_content(event.tool_name, event.arguments)
        agent._permission_checker.add_session_allow(event.tool_name, content)

    agent.resolve_hitl(event.tool_use_id, allowed, response.choice.value)


def run() -> None:
    providers = load_config("config.yaml")
    provider = select_provider(providers)
    try:
        asyncio.run(_run_async(provider))
    except KeyboardInterrupt:
        print("\nBye!")


def _make_on_replace(writer):
    """压缩整体替换时：先写 compact 标记再逐条追加新消息（F12/F44）。"""

    def _replace(msgs):
        writer.append_compact_marker()
        for m in msgs:
            writer.append(m)

    return _replace


def _build_skill_catalog_text(loader) -> str:
    """构建 Skill catalog 文本（第一阶段：名字 + 描述列表）。"""
    skills = loader.list_all()
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "",
        "You have access to the following Skills. When a user request matches "
        "a Skill's description, call the `LoadSkill` tool with the skill name "
        "to activate it and receive the full SOP.",
        "",
    ]
    for s in skills:
        lines.append(f"- **{s.meta.name}**: {s.meta.description}")
    return "\n".join(lines)


async def _run_async(provider) -> None:
    console = Console()
    workspace = Path.cwd()

    # 项目指令 + 记忆索引（启动时加载一次，F45）
    instructions = load_instructions(str(workspace))
    notes = NoteStore(workspace)
    memory_text = build_memory_index_text(notes)

    # 后台会话清理，不阻塞启动（F26）
    cleanup_task = asyncio.create_task(asyncio.to_thread(cleanup_expired, str(workspace)))

    # 命令注册中心（启动期冲突即 panic 退出，N1）
    try:
        cmd_reg = Registry()
        register_builtins(cmd_reg)
    except Exception as e:
        print(f"命令注册冲突，启动终止: {e}")
        sys.exit(1)

    mcp_pool = ConnectionPool()
    writer: Writer | None = None
    agent: Agent | None = None
    app: CodeForgeApp | None = None

    try:
        # ── 初始化 Agent ──
        client = LLMClient.create(provider)
        registry = get_default_registry()
        exec_ctx = ExecutionContext(cwd=workspace, session_id="main")
        config = AgentConfig(max_iterations=25)

        # 会话级上下文压缩运行时（跨轮复用，Agent 不再每轮重建）
        runtime = SessionRuntime(
            session=new_session_context(str(workspace)),
            context_window=effective_context_window(
                provider.protocol, provider.context_window
            ),
            notes=notes,
        )

        # 会话存档：JSONL 追加 + 压缩标记通过 Conversation 回调驱动
        writer = Writer(runtime.session.session_dir, model=provider.model)
        conversation = ConversationManager(
            system_prompt="",
            on_append=writer.append,
            on_replace=_make_on_replace(writer),
        )

        agent = Agent(
            registry=registry,
            llm_client=client,
            exec_ctx=exec_ctx,
            conversation=conversation,
            config=config,
            runtime=runtime,
            instructions=instructions,
            memory=memory_text,
        )

        # Inject ExitPlanMode tool callbacks
        try:
            from core.tool.tools.exit_plan_mode import ExitPlanModeTool
            epm = registry.get("ExitPlanMode")
            if isinstance(epm, ExitPlanModeTool):
                epm._is_plan_mode = lambda: agent.plan_mode
                epm._plan_exists = lambda: bool(agent._plan_path and agent._plan_path.exists())
        except Exception:
            pass

        # ── Skill 系统初始化 ──
        skill_loader = SkillLoader(str(workspace))
        skill_loader.load_all()

        # LoadSkill 工具（系统工具，不受白名单约束）
        load_skill_tool = LoadSkillTool()
        load_skill_tool.set_loader(skill_loader)
        load_skill_tool.set_agent(agent)
        registry.register(load_skill_tool)

        # InstallSkill 工具（远程安装）
        install_skill_tool = InstallSkillTool(catalog=skill_loader, work_dir=str(workspace))
        registry.register(install_skill_tool)

        # 校验 Skill 白名单工具存在性（fail-fast）
        removed = skill_loader.validate_tools(registry)
        if removed:
            console.print(f"[dim]Skill: {len(removed)} skill(s) removed due to missing tools: {', '.join(removed)}[/]")

        # Skill 执行器
        skill_executor = SkillExecutor(
            catalog=skill_loader,
            runtime=runtime,
            registry=registry,
            provider=provider,
            workspace=str(workspace),
        )
        skill_executor.set_agent(agent)

        # Skill Catalog 注入（第一阶段：名字 + 描述）
        agent.set_skill_catalog(_build_skill_catalog_text(skill_loader))

        # Skill → 斜杠命令自动注册
        register_skills_as_commands(cmd_reg, skill_loader, skill_executor)

        # InstallSkill 安装后回调 → 重新注册命令
        install_skill_tool.set_on_installed(
            lambda _: register_skills_as_commands(cmd_reg, skill_loader, skill_executor)
        )

        # 注册 /skill 管理命令（需要注入 SkillLoader 和 SkillExecutor）
        from functools import partial

        from core.commands.builtin_skill import handle_skill
        from core.commands.types import Command
        from core.commands.types import Kind as CmdKind

        cmd_reg.register(
            Command(
                name="skill",
                description="管理 Skill（list / info / reload）",
                kind=CmdKind.LOCAL,
                handler=partial(
                    handle_skill,
                    catalog=skill_loader,
                    executor=skill_executor,
                ),
            )
        )

        _print_banner(console, provider.name, provider.model)

        # ── 加载 MCP 外部工具 ──
        mcp_count = 0
        mcp_tool_count = 0
        mcp_configs = load_mcp_config()
        if mcp_configs:
            mcp_pool.configure(mcp_configs)
            for cfg in mcp_configs:
                try:
                    name = cfg["name"]
                    mclient = await mcp_pool.get_client(name)
                    if mclient:
                        tools = await mclient.list_tools()
                        for td in tools:
                            adapter = MCPToolAdapter(mclient, td, name)
                            registry.register(adapter)
                            mcp_tool_count += 1
                        mcp_count += 1
                        console.print(
                            f"[dim]MCP: {name} → {len(tools)} tools loaded[/]"
                        )
                except Exception:
                    pass
            if mcp_count > 0:
                console.print(
                    f"[dim]MCP: {mcp_count} servers, {mcp_tool_count} tools total[/]"
                )
                console.print()

        # ── 输入历史 + 命令补全 ──
        history = InMemoryHistory()
        session = PromptSession(
            history=history,
            completer=CommandCompleter(cmd_reg),
            complete_while_typing=True,
            key_bindings=_command_key_bindings(),
        )

        app = CodeForgeApp(
            console=console,
            agent=agent,
            session=session,
            conversation=conversation,
            runtime=runtime,
            provider=provider,
            mcp_pool=mcp_pool,
            workspace=str(workspace),
            notes=notes,
            writer=writer,
            cmd_registry=cmd_reg,
            skill_loader=skill_loader,
            skill_executor=skill_executor,
        )

        while True:
            # ── 输入 ──
            try:
                hint = f"[{app.agent.mode.value}] " if app.agent.mode.value != "off" else ""
                text = await session.prompt_async(
                    ANSI(f"\033[2m{hint}>\033[0m "),
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

            # ── 启动加载动画（首个 chunk 到达前）──
            spinner_task = asyncio.create_task(_spinner(sys.stdout))
            renderer = _StreamRenderer(console, spinner_task)

            # ── Agent 循环 ──
            app.agent_running = True
            full_text = ""
            error_occurred = False
            iteration = 0
            start_time = time.monotonic()

            try:
                async for event in app.agent.run(text):
                    if isinstance(event, ThinkingDelta):
                        renderer.on_thinking(event.text)

                    elif isinstance(event, TextDelta):
                        renderer.on_text(event.text)
                        full_text += event.text

                    elif isinstance(event, ToolCallStarted):
                        renderer.on_tool_start(event.name, event.input)

                    elif isinstance(event, ToolCallFinished):
                        renderer.on_tool_finish(
                            event.name, event.input, event.success,
                            event.result_preview, event.duration_ms,
                        )

                    elif isinstance(event, CompactEvent):
                        renderer._stop_spinner()
                        _render_compact_event(console, event)

                    elif isinstance(event, IterationUpdate):
                        iteration = event.iteration

                    elif isinstance(event, AgentFinished):
                        usage = event.total_usage
                        sys.stdout.write(
                            f"\n\033[2m({event.elapsed_s:.1f}s, "
                            f"{event.iterations}r, "
                            f"in:{usage.get('input_tokens', 0)} "
                            f"out:{usage.get('output_tokens', 0)}t)\033[0m"
                        )
                        sys.stdout.flush()

                    elif isinstance(event, AgentError):
                        renderer._stop_spinner()
                        console.print(f"[red]x {event.message} ({event.code})[/]")
                        error_occurred = True
                        app.agent_running = False

                    elif isinstance(event, HITLRequired):
                        # 权限确认 → 弹出 HITL 对话框（上下键 + 回车）
                        renderer._stop_spinner()
                        await _handle_hitl(app, event)

                    elif isinstance(event, PlanReady):
                        # Plan Mode 完成 → 审批流程
                        renderer._stop_spinner()
                        app.agent_running = False
                        await _handle_plan_ready(app, event)

            except KeyboardInterrupt:
                app.agent.cancel()
                spinner_task.cancel()
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                console.print("[yellow]Cancelled.[/]")
                app.agent_running = False
                continue

            finally:
                if not spinner_task.done():
                    spinner_task.cancel()

            app.agent_running = False

            # ── 分隔与收尾 ──
            if not error_occurred and renderer.has_started:
                sys.stdout.write("\n")
                sys.stdout.flush()

            if not renderer.has_started and not error_occurred:
                console.print(f"[dim]No response ({time.monotonic() - start_time:.1f}s)[/]")

            console.print("[dim]---[/]\n")

    finally:
        # 等待后台记忆任务 + 关闭存档 + 清理
        cur_agent = app.agent if app is not None else agent
        cur_writer = app.writer if app is not None else writer
        if cur_agent is not None:
            await cur_agent.shutdown_memory(timeout=3.0)
        if cur_writer is not None:
            cur_writer.close()
        if not cleanup_task.done():
            cleanup_task.cancel()
        try:
            await mcp_pool.close_all()
        except Exception:
            pass


async def _spinner(stream, interval: float = 0.2) -> None:
    """后台加载动画，在首个 chunk 到达前持续刷新。"""
    frames = ["", ".", "..", "...", " ..", "  ..", "   ..."]
    i = 0
    try:
        while True:
            stream.write(f"\r\033[K\033[2m{''.join(frames[i % len(frames)])}\033[0m")
            stream.flush()
            i += 1
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
