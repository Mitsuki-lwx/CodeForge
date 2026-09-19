"""CodeForge 终端对话 —— ch04 Agent Loop 版。

基于 core.agent.Agent 的事件流模型：
Agent 自主多轮调用工具直到任务完成，UI 只消费事件。"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console

from config.loader import load_config, load_host_config
from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent import Agent
from core.agent.bootstrap import (
    CommandRegistrationError,
    ReuseContext,
    SessionBundle,
    build_session,
)
from core.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    CompactEvent,
    CompactPhase,
    HITLRequired,
    PlanReady,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from core.agent.runtime import SessionRuntime
from core.archive import (
    Writer,
    cleanup_expired,
)
from core.archive import (
    make_on_replace as _make_on_replace,
)
from core.commands import Kind, parse_line
from core.context_compression.state import new_session_context
from core.hooks import HookContext
from core.mcp import ConnectionPool
from core.permissions.hitl import HITLChoice
from core.permissions.modes import PermissionMode
from core.permissions.rules import extract_content
from core.permissions.upgrade import ApprovalUpgrader
from tui.completer import CommandCompleter
from tui.hitl_dialog import show_hitl_dialog
from tui.provider_select import select_provider
from tui.select import read_line, select_from_options

CAT_ASCII = r"""      /\_/\
     (o.o)
     > ^ <"""


def _detect_language(text: str) -> str:
    """检测文本主导语言。返回 'zh'、'en' 或 ''（无法判断）。"""
    chinese_chars = sum(1 for c in text if "一" <= c <= "鿿")
    english_chars = sum(1 for c in text if c.isascii() and c.isalpha())
    if chinese_chars == 0 and english_chars == 0:
        return ""
    return "zh" if chinese_chars > english_chars else "en"


_LANG_HINTS = {
    "zh": "\n\n[System: Reply in Chinese (中文)]",
    "en": "\n\n[System: Reply in English]",
}


def format_compact_notice(before: int, after: int) -> str:
    """压缩完成后的统一系统消息文案。"""
    return f"已压缩，token 从 {before} 降至 {after}"


def _command_key_bindings() -> KeyBindings:
    """回车行为：补全菜单打开且有高亮命令 → 直接执行；否则正常提交。"""

    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event):
        buf = event.current_buffer
        if (
            buf.complete_state is not None
            and buf.complete_state.current_completion is not None
        ):
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
    notes: object | None = None  # core.notes.NoteStore 实例
    writer: object | None = None  # core.archive.Writer 实例
    cmd_registry: object | None = None  # core.commands.Registry 实例
    agent_running: bool = False  # 主循环是否正在跑 Agent（idle 判定）
    skill_loader: object | None = None  # core.skills.SkillLoader 实例
    skill_executor: object | None = None  # core.skills.SkillExecutor 实例
    hook_runner: object | None = None  # core.hooks.HookRunner 实例
    task_mgr: object | None = None  # core.task.manager.BackgroundTaskManager 实例
    subagent_catalog: object | None = None  # core.agent.role_loader.Catalog 实例
    wt_manager: object | None = None  # core.worktree.manager.WorktreeManager 实例
    team_mgr: object | None = None  # core.team.manager.Manager 实例
    state: object | None = None  # core.notes.state.SessionStateStore | None
    provider_list: list = field(default_factory=list)  # 已加载的 provider 列表（/model 用）
    router_cfg: object | None = None  # config.model.RouterConfig | None（路由配置）
    # 审批升级通道（core.permissions.upgrade.ApprovalUpgrader）：
    # 前台子 Agent 的 ask 经它冒泡到这里弹窗。`/resume` 换会话时要把它
    # 一起传进新的 build_session，否则恢复后的会话里子 Agent 审批会失联。
    approval_upgrader: object | None = None

    # ── UI Protocol 实现（handler 通过此接口操作 TUI）──────────────

    def router_cheap_tier(self) -> str | None:
        """路由启用时返回便宜价位标记；未启用返回 None。"""
        if self.router_cfg is not None and getattr(self.router_cfg, "enabled", False):
            return getattr(self.router_cfg, "cheap_tier", "") or "cheap"
        return None

    def providers(self) -> list:
        return self.provider_list

    def switch_model(self, provider) -> None:
        self.agent.switch_model(provider)
        self.provider = provider  # 路由判定用当前主模型

    def session_state(self) -> object | None:
        return self.state

    async def mcp_list(self) -> list[dict]:
        if self.mcp_pool is None:
            return []
        try:
            all_tools = await self.mcp_pool.list_all_tools()
        except Exception:  # noqa: BLE001 —— 查询失败返回空，不阻断
            return []
        return [{"name": name, "tools": tools} for name, tools in all_tools.items()]

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
            self.agent._permission_checker.plan_file_path = str(self.agent._plan_path)

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

    def _hook_payload(self, event: str) -> HookContext:
        """构造会话级 hook 事件上下文（通用字段）。"""
        return HookContext(
            event=event,
            session_id=self.session_id(),
            cwd=str(self.workspace),
            mode=self.mode().value,
        )

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
        options = [f"{it.title}  [{it.relative_time}]  {it.model}" for it in items]
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
        # ── Hook: 触发 session_end（旧会话），reset 前捕获会话上下文 ──
        if self.hook_runner is not None:
            try:
                asyncio.create_task(
                    self.hook_runner.run(
                        "session_end", self._hook_payload("session_end")
                    )
                )
            except RuntimeError:
                pass  # 事件循环未运行时不触发
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
            result.append(
                {
                    "name": s.meta.name,
                    "description": s.meta.description,
                    "source": self.skill_loader.get_source_label(s.meta.name),
                    "mode": s.meta.mode,
                }
            )
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

    # ── Hook 系统 ──

    def hook_sources(self) -> list[str]:
        return self.hook_runner.sources if self.hook_runner else []

    def hook_rules(self) -> list:
        return self.hook_runner.rules if self.hook_runner else []

    # ── Worktree 系统 ──

    def worktree_list(self) -> list[dict]:
        """列出全部活跃 worktree。"""
        if self.wt_manager is None:
            return []
        sessions = self.wt_manager.list_active()
        return [
            {
                "name": s.name,
                "owner": s.owner,
                "path": s.path,
                "branch": s.branch,
            }
            for s in sessions
        ]

    async def worktree_create(self, name: str) -> dict:
        """创建（或快速恢复）一个 worktree。"""
        if self.wt_manager is None:
            return {"ok": False, "reason": "Worktree system not initialized"}
        try:
            s = await self.wt_manager.create(name, owner="user")
            return {"ok": True, "name": s.name, "path": s.path}
        except Exception as e:  # noqa: BLE001 —— 创建失败转错误结果
            return {"ok": False, "reason": str(e)}

    async def worktree_enter(self, name: str) -> str:
        """进入一个 worktree（设置 agent 的显式 cwd）。"""
        if self.wt_manager is None:
            return "Worktree system not initialized"
        try:
            s = await self.wt_manager.enter(name)
        except Exception as e:  # noqa: BLE001 —— 进入失败转错误
            return str(e)
        # 设置主 Agent 的显式 cwd 到 worktree
        try:
            self.agent._exec_ctx.cwd = __import__("pathlib").Path(s.path)
        except Exception:  # noqa: BLE001, S110 —— cwd 替换失败仍视为已进入
            pass
        return ""

    async def worktree_exit(self, name: str) -> str:
        """退出一个 worktree（清注册表 + 恢复主 Agent cwd）。"""
        if self.wt_manager is None:
            return "Worktree system not initialized"
        try:
            await self.wt_manager.exit(name)
        except Exception as e:  # noqa: BLE001 —— exit 失败转错误
            return str(e)
        # 恢复主 Agent cwd 到主目录
        try:
            self.agent._exec_ctx.cwd = __import__("pathlib").Path(self.workspace)
        except Exception:  # noqa: BLE001, S110 —— cwd 恢复失败仍视为已退出
            pass
        return f"Exited worktree: {name}"

    async def worktree_delete(self, name: str) -> str:
        """删除一个 worktree（含变更保护）。"""
        if self.wt_manager is None:
            return "Worktree system not initialized"
        try:
            ok, reason = await self.wt_manager.delete(name)
        except Exception as e:  # noqa: BLE001 —— 删除失败转错误
            return str(e)
        if not ok:
            return reason
        return ""

    # ── Team 系统（/team 命令的 UI Protocol 实现）───────────────────

    def team_list(self) -> list[dict]:
        """列出全部 Team 摘要。"""
        if self.team_mgr is None:
            return []
        return [
            {
                "name": t.name,
                "backend": t.backend.value,
                "members": [m.to_dict() for m in t.members],
                "config_path": t.config_path,
            }
            for t in self.team_mgr.list_()
        ]

    def team_info(self, name: str) -> dict | None:
        """按名取 Team 详情。"""
        if self.team_mgr is None:
            return None
        team = self.team_mgr.get(name)
        if team is None:
            return None
        return {
            "name": team.name,
            "backend": team.backend.value,
            "config_path": team.config_path,
            "members": [m.to_dict() for m in team.members],
        }

    async def team_delete(self, name: str, force: bool = False) -> str:
        """删除 Team；非 force 且活跃成员时返回错误。"""
        if self.team_mgr is None:
            return "Team 系统未初始化"
        try:
            await self.team_mgr.delete(name, force=force)
        except Exception as e:  # noqa: BLE001 —— 删除失败转错误
            return str(e)
        return ""

    async def team_kill(self, member: str) -> str:
        """杀一个成员：查到其所属 Team/agent_id → 后端 kill → 移除。"""
        if self.team_mgr is None:
            return "Team 系统未初始化"
        found = False
        for team in self.team_mgr.list_():
            info = team.member_by_name(member)
            if info is not None:
                found = True
                try:
                    await self.team_mgr._kill_member(info)
                    await self.team_mgr.remove_member(team, member)
                except Exception as e:  # noqa: BLE001 —— 杀成员失败转错误
                    return str(e)
                break
        return "" if found else f"未找到成员 {member}"

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
        """恢复指定会话：还原对话后走统一装配入口重建会话（F22）。

        装配交给 `core/agent/bootstrap.build_session`——与新建会话同一条路径。
        此前这里手搓一个裸 Agent（只有 `get_default_registry()`，不传 hooks、
        不建会话状态、不注入 skill catalog、不设 loop），恢复出来的会话能力集
        弱于新会话；现在两条路径共用装配，差异不再可能出现。

        Args:
            session_id: 会话 ID（YYYYMMDD-HHMMSS-xxxx）。

        Returns:
            恢复提示文本，如「已恢复会话 <id>，共 <N> 条消息」。
        """
        from core.archive import restore_session

        session_dir = Path(self.workspace) / ".codeforge" / "sessions" / session_id
        result = await restore_session(
            session_dir,
            provider_config=self.provider,
            context_window=self.runtime.context_window,
        )

        # 旧会话的后台子 Agent 属于旧对话，切走前取消，避免结果注入到新会话
        if self.task_mgr is not None:
            try:
                await self.task_mgr.cancel_all()
            except Exception:  # noqa: BLE001 —— 取消失败不阻断恢复
                pass

        bundle = await build_session(
            provider=self.provider,
            workspace=self.workspace,
            session_dir=session_dir,
            conversation=result.conversation,
            # 复用长生命周期资源：换掉 task_mgr 会让后台通知消费协程失去订阅，
            # 换掉 mcp_pool 会重新拉起 MCP 子进程并泄漏旧池（见 ReuseContext）
            reuse=ReuseContext(
                mcp_pool=self.mcp_pool,
                task_mgr=self.task_mgr,
                wt_manager=self.wt_manager,
                notes=self.notes,
            ),
            # 同一个通道实例要带过去：`/resume` 会重建 exec_ctx，不带的话恢复后的
            # 会话里前台子 Agent 的审批就失联了（消费者 Task 绑的是同一个 console，
            # 无需重启，只是新 session 得认得它）。
            approval_upgrader=self.approval_upgrader,
        )

        # 旧 writer 关闭；旧会话 JSONL 保留不删（F24）
        if self.writer is not None:
            self.writer.close()

        # 整体切换：不能只换 agent/conversation，否则 app 里其余引用
        # （cmd / skill / state / team / hooks）仍指向旧会话
        self.agent = bundle.agent
        self.conversation = bundle.conversation
        self.runtime = bundle.runtime
        self.writer = bundle.writer
        self.cmd_registry = bundle.cmd_registry
        self.skill_loader = bundle.skill_loader
        self.skill_executor = bundle.skill_executor
        self.task_mgr = bundle.task_mgr
        self.subagent_catalog = bundle.subagent_catalog
        self.wt_manager = bundle.wt_manager
        self.team_mgr = bundle.team_mgr
        self.state = bundle.state_store
        self.notes = bundle.notes
        self.hook_runner = bundle.hook_runner

        for notice in bundle.notices:
            self.println(notice)

        # 补全器绑定的是旧命令注册中心；skill 变化会让两者内容不同，重绑一次。
        # 无头占位 session 不支持该属性，忽略即可。
        try:
            self.session.completer = CommandCompleter(bundle.cmd_registry)
        except Exception:  # noqa: BLE001 —— 占位 session 不支持补全器
            pass

        return (
            f"已恢复会话 {session_id}，"
            f"共 {len(bundle.conversation.messages)} 条消息"
        )


class _HeadlessSessionStub:
    """无头单任务模式的 placeholder，顶替 PromptSession。

    任务模式不进入交互循环、不调 `prompt_async`/补全，故无需真实 prompt_toolkit
    会话（后者在 GUI-less shell 会因无 Windows 屏幕缓冲区抛 NoConsoleScreenBufferError）。
    仅占位满足 `CodeForgeApp.session` 字段；任何误用 `prompt_async` 会给出明确错误。
    """

    async def prompt_async(self, *a, **k):
        raise RuntimeError("无头任务模式不提供交互输入")

    def __getattr__(self, name):
        raise AttributeError(f"_HeadlessSessionStub 无 {name}（任务模式仅占位）")


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
        self.has_started = False  # 首个 Thinking/Text/Tool 到达，spinner 已停
        self.in_thinking = False  # 当前正处在思考块

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

    def on_tool_finish(
        self, name: str, input: dict, success: bool, preview: str, duration_ms: int
    ) -> None:
        icon = "[green]✓[/]" if success else "[red]✗[/]"
        dur = f" ({duration_ms / 1000:.1f}s)" if duration_ms else ""
        line = f"     {icon} {_tool_title(name, input)}{dur}"
        if not success:
            err = _short_preview(preview, 80)
            if err:
                line += f"  [dim]{err}[/]"
        self.console.print(line)


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


def _progress_hint(app: CodeForgeApp) -> str:
    """看门狗静默时说明「在等谁、等什么」，而不是笼统说"等模型响应"。

    父 Agent 执行工具（含子 Agent）期间自己的事件流是静默的 —— 生成器挂在
    `await` 上、不能 yield（结构性事实，实测确认过）。所以这里**主动去读**
    子 Agent 留下的进度记录（`core/tool/context.ProgressSink`）；读不到就退回原说法。

    实测踩过的误导：派了子 Agent 之后界面一直显示"仍在等待模型响应"，
    让人以为上游卡住，实际是子 Agent 在跑。
    """
    ctx = getattr(getattr(app, "agent", None), "_exec_ctx", None)
    sink = getattr(ctx, "progress", None)
    note = sink.snapshot() if sink is not None else None
    if note is None:
        return "模型响应"
    who = f"子 Agent「{note.agent}」" if note.agent else "子 Agent"
    what = note.action or "工作中"
    if note.detail:
        what = f"{what} {note.detail}"
    return f"{who}（{what}）"


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


def _render_compact_event(
    console: Console,
    event: CompactEvent,
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

    agent.resolve_hitl(event.tool_use_id, allowed, response.choice.value)


def run(task: str = "", loop: str = "", host: bool | None = None) -> None:
    """启动 TUI；`host` 打开时改为 host 客户端模式。

    `host` 为 `None` 表示"没显式指定"→ 取 `features.host.enabled`（默认关）。
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
        asyncio.run(_run_async(provider, task=task, providers=providers, loop=loop))
    except KeyboardInterrupt:
        print("\nBye!")


async def _run_async(provider, task: str = "", providers=None, loop: str = "") -> None:
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
                approval_upgrader=_approval_upgrader,
            )
        except CommandRegistrationError as e:
            print(f"命令注册冲突，启动终止: {e}")
            sys.exit(1)

        for _notice in bundle.notices:
            console.print(f"[dim]{_notice}[/]")

        _print_banner(console, provider.name, provider.model)

        # ── 输入历史 + 命令补全 ──
        # 无头单任务模式不进入交互循环：跳过 PromptSession（GUI-less shell 会因
        # 无 Windows 屏幕缓冲区抛 NoConsoleScreenBufferError），用最小占位。
        history = InMemoryHistory()
        if task:
            session = _HeadlessSessionStub()
        else:
            session = PromptSession(
                history=history,
                completer=CommandCompleter(bundle.cmd_registry),
                complete_while_typing=True,
                key_bindings=_command_key_bindings(),
            )

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
            _approval_upgrader.start(
                lambda req: show_hitl_dialog(app.console, req)
            )

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
            getattr(bundle.team_features, "router", None) if bundle.team_features else None
        )
        app.router_cfg = _router_cfg  # 暴露给 /model：切到 cheap 时提示路由停用
        _router_enabled = bool(_router_cfg and _router_cfg.enabled)
        _router_prompt = getattr(_router_cfg, "judge_prompt", "") if _router_cfg else ""
        _router_cheap = (
            getattr(_router_cfg, "cheap_tier", "cheap") if _router_cfg else "cheap"
        )

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
                app.provider_list, app.provider,
                enabled=_router_enabled, cheap_tier=_router_cheap,
            )
            if _router is not None:
                _cheap, _ = _router
                _kind, _answer = await _judge_route(
                    _cheap, text, judge_prompt=_router_prompt or None
                )
                if _kind == "simple" and _answer:
                    console.print(f"[cyan]➤ {getattr(_cheap, 'name', '便宜')}[/] {_answer}")
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
        cur_agent = app.agent if app is not None else (
            bundle.agent if bundle is not None else None
        )
        cur_writer = app.writer if app is not None else (
            bundle.writer if bundle is not None else None
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
