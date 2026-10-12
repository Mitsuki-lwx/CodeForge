"""CodeForge 终端对话 —— ch04 Agent Loop 版。

基于 core.agent.Agent 的事件流模型：
Agent 自主多轮调用工具直到任务完成，UI 只消费事件。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit import PromptSession
from rich.console import Console

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from core.agent import Agent
from core.agent.bootstrap import (
    ReuseContext,
    build_session,
)
from core.agent.runtime import SessionRuntime
from core.archive import (
    Writer,
)
from core.archive import (
    make_on_replace as _make_on_replace,
)
from core.commands import Kind, parse_line
from core.context_compression.state import new_session_context
from core.hooks import HookContext
from core.mcp import ConnectionPool
from core.permissions.modes import PermissionMode
from tui.app_inspect import InspectionMixin
from tui.app_skills import SkillsHooksMixin
from tui.app_workspace import WorkspaceCommandsMixin
from tui.completer import CommandCompleter
from tui.render import (
    format_compact_notice,
)
from tui.select import select_from_options


@dataclass
class CodeForgeApp(InspectionMixin, SkillsHooksMixin, WorkspaceCommandsMixin):
    """TUI 应用状态容器，命令处理函数与事件循环共享。

    命令实现按关注点拆到 Mixin（各自一个文件），本文件只留状态字段、
    跨命令共用的取数与会话生命周期：

    - `InspectionMixin`（app_inspect.py）—— `/agents`、`/show`
    - `SkillsHooksMixin`（app_skills.py）—— `/skill`、`/hooks` 的只读视图
    - `WorkspaceCommandsMixin`（app_workspace.py）—— `/worktree`、`/team`
    """

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
    # 排队中的"对某个队友说的话"（task_id → 消息列表）。任务跑完时由
    # `_consume_task_done` 投递；不持久化，进程退出即丢（spec_teammate_inspect §3.4）。
    pending_agent_messages: dict[str, list[str]] = field(default_factory=dict)
    subagent_catalog: object | None = None  # core.agent.role_loader.Catalog 实例
    wt_manager: object | None = None  # core.worktree.manager.WorktreeManager 实例
    team_mgr: object | None = None  # core.team.manager.Manager 实例
    state: object | None = None  # core.notes.state.SessionStateStore | None
    provider_list: list = field(
        default_factory=list
    )  # 已加载的 provider 列表（/model 用）
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

    def print_markup(self, msg: str) -> None:
        """按普通输出打印（**不套 dim**）—— 供列表/transcript 这类正文用。"""
        self.console.print(msg)

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
        # 延迟 import：`tui.session` 需要 `CodeForgeApp`，模块级 import 会成环
        from tui.session import _inject_and_run

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
        from tui.session import _inject_and_run

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

        return f"已恢复会话 {session_id}，共 {len(bundle.conversation.messages)} 条消息"
