"""Agent 工具 —— 统一的子 Agent 委派入口。

主 Agent 通过此工具把子任务委派给独立子 Agent 执行。
用 subagent_type 参数分流定义式和 Fork 式两条路径。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult

logger = logging.getLogger(__name__)


@dataclass
class AgentArgs:
    """Agent 工具参数。"""

    prompt: str = ""
    description: str = ""
    subagent_type: str = ""
    model: str = ""
    run_in_background: bool = False
    name: str = ""
    team_name: str = ""


def _render_state_snapshot(store) -> str:
    """渲染父会话状态快照（目标+约束+待办），供子 agent 只读继承。"""
    try:
        cons = store.list_constraints(include_persisted=True)
        goal = store.get_goal()
        todos = store.list_todos()
        parts: list[str] = []
        if cons:
            parts.append(
                "## 父会话硬性约束（必须遵守）\n"
                + "\n".join(f"- {c['text']}" for c in cons)
            )
        if goal:
            parts.append(f"## 父会话目标\n{goal}")
        if todos:
            parts.append(
                "## 父会话待办\n"
                + "\n".join(
                    f"- [{'x' if t['done'] else ' '}] {t['text']}" for t in todos
                )
            )
        return "\n\n".join(parts)
    except Exception:  # noqa: BLE001 —— 快照渲染失败静默
        return ""


class AgentTool(Tool):
    """统一的 Agent 委派工具。

    用法：
        agent(
            prompt="...",            # 必填
            description="...",      # 必填
            subagent_type="...",    # 可选，留空走 Fork
            model="...",            # 可选
            run_in_background=True, # 可选
            name="...",             # 可选
        )
    """

    timeout_seconds = 180.0  # 比前台超时更长，为后台留足时间

    def __init__(
        self,
        catalog: object | None = None,
        task_mgr: object | None = None,
        parent_agent: object | None = None,
        bg_enabled: bool = True,
        wt_manager: object | None = None,
        team_hook: object | None = None,
    ) -> None:
        self._catalog = catalog
        self._task_mgr = task_mgr
        self._parent = parent_agent
        self._bg_enabled = bg_enabled
        self._wt_manager = wt_manager
        self._team_hook = team_hook  # TeamHook：team_name 分支委托

    def set_parent(self, agent: object) -> None:
        """回填父 Agent 引用（启动期 wiring）。"""
        self._parent = agent

    def set_wt_manager(self, wt_manager: object) -> None:
        """注入 WorktreeManager（启动期 wiring，isolation 角色用）。"""
        self._wt_manager = wt_manager

    def set_team_hook(self, team_hook: object) -> None:
        """注入 TeamHook（wiring，team_name 分支用）。"""
        self._team_hook = team_hook

    def name(self) -> str:
        return "Agent"

    def description(self) -> str:
        base = (
            "Launch a sub-agent to handle complex, multi-step tasks independently. "
            "Use subagent_type to select a predefined role (Explore, Plan, general-purpose), "
            "or leave empty to fork the current conversation."
        )
        # 动态追加可用角色列表
        if self._catalog is not None:
            try:
                roles = self._catalog.list()
                if roles:
                    names = ", ".join(r.name for r in roles if not r.is_fork())
                    base += f" Available types: {names}."
            except Exception:  # noqa: BLE001, S110 —— 角色列表失败仅导致描述缺少类型名
                pass
        return base

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task for the sub-agent to perform.",
                },
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task.",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "The type of pre-defined sub-agent to use (e.g. 'Explore', 'Plan'). Leave empty to fork.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override: 'haiku', 'sonnet', 'opus', or 'inherit'.",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "Set to true to run the sub-agent in the background.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the sub-agent (used with SendMessage to continue it later).",
                },
                "team_name": {
                    "type": "string",
                    "description": "Non-empty to spawn this agent as a long-running teammate in the given team (created via TeamCreate).",
                },
            },
            "required": ["prompt", "description"],
        }

    def is_read_only(self) -> bool:
        return False  # 子 Agent 可能做任何事

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        # ── 1. 解析参数 ──
        args = AgentArgs(
            prompt=input.get("prompt", ""),
            description=input.get("description", ""),
            subagent_type=input.get("subagent_type", ""),
            model=input.get("model", ""),
            run_in_background=input.get("run_in_background", False),
            name=input.get("name", ""),
            team_name=input.get("team_name", ""),
        )

        # ── 1.5 团队分支：team_name 非空 → 派生队员 ──
        if args.team_name:
            if self._team_hook is None:
                return ToolResult(
                    success=False,
                    error="team_name 需要 TeamHook 已注入（团队系统未启用）",
                )
            from core.agent.team_hook import current_teammate

            # in-process 队员不能再派生队员（防嵌套，F19）
            tc = current_teammate()
            if tc is not None and tc.backend_type == "in-process":
                return ToolResult(
                    success=False,
                    error="in-process 队员不能再次派生队员（InProcessTeammateNoSpawnError）",
                )
            try:
                out = await self._team_hook.spawn_teammate(
                    parent_agent=self._parent or object(),
                    worktree_mgr=self._wt_manager,
                    task_mgr=self._task_mgr,
                    team_name=args.team_name,
                    member_name=args.name or args.subagent_type or "worker",
                    prompt=args.prompt,
                    subagent_type=args.subagent_type,
                    model=args.model,
                )
            except Exception as e:
                logger.exception("team spawn failed")
                return ToolResult(success=False, error=f"团队派生失败: {e}")
            return ToolResult(success=True, data=out)

        if not args.prompt:
            return ToolResult(success=False, error="prompt is required")
        if not args.description:
            return ToolResult(success=False, error="description is required")

        # ── 2. 防嵌套：检测是否已在子 Agent 上下文中 ──
        parent = self._parent
        if parent is not None:
            parent_conv = getattr(parent, "_conversation", None)
            if parent_conv is not None:
                try:
                    from core.agent.fork import is_fork_context

                    if is_fork_context(parent_conv.messages):
                        return ToolResult(
                            success=False,
                            error="Fork sub-agent cannot spawn another Agent (boilerplate detected)",
                        )
                except Exception:  # noqa: BLE001, S110 —— fork 检测失败则不拦截，靠工具过滤兜底
                    pass

        # ── 3. 解析角色定义 ──
        if self._catalog is None:
            return ToolResult(success=False, error="Agent catalog not initialized")

        try:
            if args.subagent_type:
                role = self._catalog.resolve(args.subagent_type)
                if role is None:
                    return ToolResult(
                        success=False,
                        error=f"Unknown subagent_type: {args.subagent_type}",
                    )
            else:
                role = self._catalog.fork_role()
        except Exception as e:  # noqa: BLE001 —— 角色解析失败转错误结果
            return ToolResult(success=False, error=f"Failed to resolve agent role: {e}")

        is_fork = role.is_fork()

        # ── 4. 决定后台模式 ──
        background = role.background or args.run_in_background or is_fork
        if background and not self._bg_enabled:
            if is_fork:
                return ToolResult(
                    success=False,
                    error="Fork requires background mode, which is disabled by config",
                )
            # 非 fork：降级为前台
            background = False

        # ── 5. 工具过滤 ──
        allowed = await self._apply_filter(role, is_fork, background)

        # ── 5.5. isolation: 创建/进入 worktree ──
        isolation = bool(getattr(role, "isolation", False))
        wt_session = None
        if isolation:
            try:
                wt_session = await self._ensure_worktree(context.session_id)
            except Exception as e:
                logger.exception("Failed to set up worktree isolation")
                return ToolResult(
                    success=False,
                    error=f"Failed to set up worktree isolation: {e}",
                )

        # ── 6. 构造子 Agent ──
        try:
            # 可读身份名：优先 Agent 工具的 name，其次角色名，兜底 'sub'（供 span 归属）
            sub_name = args.name or (getattr(role, "name", "") or "sub")
            sub_agent = self._build_sub_agent(
                role,
                allowed,
                context.session_id,
                wt_session,
                is_fork=is_fork,
                name=sub_name,
            )
        except Exception as e:
            logger.exception("Failed to build sub-agent")
            return ToolResult(success=False, error=f"Failed to build sub-agent: {e}")

        # ── 7. 构造子对话 ──
        from conversation.manager import ConversationManager

        if is_fork:
            try:
                from core.agent.fork import build_forked_messages

                parent_msgs = getattr(parent, "_conversation", None)
                if parent_msgs is not None:
                    forked = build_forked_messages(parent_msgs.messages, args.prompt)
                    sub_conv = ConversationManager()
                    sub_conv.replace_history(forked)
                else:
                    sub_conv = ConversationManager()
                    sub_conv.add_user_message(args.prompt)
            except Exception as e:  # noqa: BLE001 —— 消息克隆失败转错误结果
                return ToolResult(success=False, error=f"Failed to fork messages: {e}")
        else:
            sub_conv = ConversationManager()

        # 记录 worktree session 到子 Agent（run_to_completion 结束 cleanup）
        if wt_session is not None:
            sub_agent._worktree_session = wt_session
            sub_agent._wt_manager = self._wt_manager

        # ── 8. 执行 ──
        if background:
            return await self._run_background(sub_agent, sub_conv, args)
        else:
            return await self._run_foreground(sub_agent, sub_conv, args)

    # ── 内部 ────────────────────────────────────────────────────────

    async def _ensure_worktree(self, session_id: str) -> object:
        """为隔离子 Agent 创建/进入 worktree。

        Returns:
            WorktreeSession；进入后其 path 为子 Agent 的 cwd。

        Raises:
            RuntimeError: wt_manager 未注入。
        """
        if self._wt_manager is None:
            raise RuntimeError("WorktreeManager not injected for isolation role")

        from core.worktree.safe_name import generate_agent_name

        name = generate_agent_name()
        wt = await self._wt_manager.create(name, owner="agent")
        await self._wt_manager.enter(name)
        return wt

    async def _apply_filter(
        self,
        role: object,
        is_fork: bool,
        background: bool,
    ) -> list[str]:
        """应用工具过滤多层防线。"""
        from core.tool.filter import FilterParams, apply_agent_tool_filter

        parent = self._parent
        all_names: list[str] = []
        if parent is not None:
            try:
                all_names = [t.name() for t in parent._registry.list()]
            except Exception:  # noqa: BLE001, S110 —— 父工具枚举失败则子 Agent 工具集为空
                pass

        p = FilterParams(
            all=all_names,
            source=int(getattr(role, "source", 0)),
            background=background,
            allowed=list(getattr(role, "tools", [])) if not is_fork else [],
            disallowed=list(getattr(role, "disallowed_tools", [])),
        )

        # Fork 路径：保留 Agent 工具（靠 QuerySource + Boilerplate 拦截）
        if is_fork:
            # Fork 不去除 Agent 工具 → 临时从全局禁止列表移除
            # 实际通过 _apply_filter 定制
            result = list(p.all)
            if p.disallowed:
                result = [t for t in result if t not in p.disallowed]
            if p.allowed:
                result = [t for t in result if t in p.allowed]
            return result

        return apply_agent_tool_filter(p)

    def _build_sub_agent(
        self,
        role: object,
        allowed: list[str],
        session_id: str,
        wt_session: object | None = None,
        is_fork: bool = False,
        name: str = "",
    ) -> object:
        """构造子 Agent 实例。

        Args:
            role: 角色定义。
            allowed: 过滤后的工具名列表。
            session_id: 父会话 ID。
            wt_session: 可选的 WorktreeSession；非空时子 Agent 工作在隔离目录。
        """
        from pathlib import Path

        from core.agent.agent import Agent
        from core.agent.config import AgentConfig
        from core.agent.runtime import SessionRuntime
        from core.context_compression.state import new_session_context
        from core.permissions.modes import PermissionMode
        from core.tool.context import ExecutionContext

        parent = self._parent
        if parent is None:
            raise RuntimeError("Parent agent not set — call set_parent() first")

        # 子 Agent 配置
        max_turns = int(getattr(role, "max_turns", 0))
        # Fork 子 Agent：创建时就定成自主执行（permission=bypass）——fork 继承父全工具
        # 又要自主干完，执行时不该逐工具 ask（对齐参考实现 mewcode 的 fork
        # `permission_mode="bypassPermissions"`；消除子 Agent 写文件卡在 HITL ask 挂起）。
        # 定义式角色子 Agent 尊重其角色声明的权限模式。
        if is_fork:
            permission_mode = PermissionMode.BYPASS
            dont_ask = True
        else:
            permission_mode = getattr(role, "permission_mode", PermissionMode.DEFAULT)
            dont_ask = bool(getattr(role, "dont_ask", False))
        system_prompt = str(getattr(role, "system_prompt", ""))

        # 工作目录：隔离时用 worktree 路径，否则用父的 workdir
        workspace = str(
            getattr(parent, "_exec_ctx", ExecutionContext(cwd=Path.cwd())).cwd
        )
        work_cwd = Path(workspace)
        if wt_session is not None:
            wt_path = getattr(wt_session, "path", "")
            if wt_path:
                work_cwd = Path(wt_path)

        # 独立的 SessionRuntime
        sub_session = new_session_context(str(work_cwd))
        sub_runtime = SessionRuntime(
            session=sub_session,
            context_window=getattr(
                getattr(parent, "_runtime", None), "context_window", 200000
            ),
            notes=None,  # 子 Agent 不触发记忆更新
        )

        # 过滤后的工具 registry
        from core.tool.registry import ToolRegistry

        # 子 agent 只读继承：排除状态写工具（SetGoal/AddTodo/AddConstraint），
        # 目标/约束以只读快照注入 system（spec_session_state）
        _STATE_TOOLS = frozenset({"SetGoal", "AddTodo", "AddConstraint"})
        sub_registry = ToolRegistry()
        for tool_name in allowed:
            if tool_name in _STATE_TOOLS:
                continue
            try:
                sub_registry.register(parent._registry.get(tool_name))
            except Exception:  # noqa: BLE001, S110 —— 单个工具注册失败跳过
                pass

        sub_exec_ctx = ExecutionContext(
            cwd=work_cwd,
            session_id=f"{session_id}-sub",
        )

        # 隔离时注入 worktree 路径说明到 system prompt
        if wt_session is not None and system_prompt:
            wt_path = getattr(wt_session, "path", "")
            if wt_path:
                system_prompt = (
                    system_prompt
                    + f"\n\nYou are working in an isolated git worktree at {wt_path}.\n"
                    "All file operations happen there. Use this directory as your working directory."
                )

        # 注入父会话的目标+约束只读快照（spec_session_state）
        parent_state = getattr(parent, "_state_store", None)
        if parent_state is not None:
            snapshot = _render_state_snapshot(parent_state)
            if snapshot:
                system_prompt = (
                    (system_prompt + "\n\n" + snapshot) if system_prompt else snapshot
                )

        sub_agent = Agent(
            registry=sub_registry,
            llm_client=getattr(parent, "_client", None),
            exec_ctx=sub_exec_ctx,
            conversation=__import__(
                "conversation.manager", fromlist=["ConversationManager"]
            ).ConversationManager(),
            config=AgentConfig(max_iterations=25),
            runtime=sub_runtime,
            system_prompt=system_prompt if system_prompt else None,
            max_turns=max_turns,
            permission_mode=permission_mode,
            dont_ask=dont_ask,
            hooks=getattr(parent, "_hooks", None),
            loop=getattr(parent, "_loop", None),  # 子 agent 继承父 loop（spec_loop）
        )
        # 可读身份名注入（span 归属 `codeforge.agent.name`）
        if name:
            sub_agent.set_agent_name(name)

        return sub_agent

    async def _run_foreground(
        self,
        sub_agent: object,
        sub_conv: object,
        args: AgentArgs,
    ) -> ToolResult:
        """前台同步执行子 Agent——等到它跑完，直接把结果文本返回给 Lead。

        对齐参考实现 mewcode：前台子 agent 同步 await run_to_completion，不设超时、
        不自动转后台。子 agent 时长由自身 AgentConfig(max_iterations) 约束；Lead 拿到
        真结果文本，因而无需 sleep+ls 轮询（消除本轮 benchmark 发现的 ~72s 空转）。
        结果文本附加防轮询提示，防止后台/超时的场景引导模型误轮询。
        """
        from core.agent.sub_agent import run_to_completion

        events: asyncio.Queue = asyncio.Queue(maxsize=64)

        final_text = await run_to_completion(sub_agent, sub_conv, args.prompt, events)
        # 子 agent 出错（run_to_completion 返回 "Error: ..."）→ 结构化 success=False，
        # 让 Lead 明确知道委派失败，而非拿到一段错误文本当成功。
        if final_text.startswith("Error:"):
            return ToolResult(success=False, error=final_text)
        return ToolResult(success=True, data=final_text)

    async def _run_background(
        self,
        sub_agent: object,
        sub_conv: object,
        args: AgentArgs,
    ) -> ToolResult:
        """后台异步启动子 Agent。"""
        if self._task_mgr is None:
            return ToolResult(
                success=False,
                error="Background task manager not available",
            )

        try:
            task_id = await self._task_mgr.launch(
                sub_agent,
                sub_conv,
                args.name,
                args.prompt,
            )
            return ToolResult(
                success=True,
                data=json.dumps(
                    {
                        "task_id": task_id,
                        "status": "async_launched",
                        # 防模型误轮询：系统会主动投递完成结果，别 sleep+ls
                        "hint": (
                            "The system will notify automatically when it completes. "
                            "Do NOT wait, sleep, or poll."
                        ),
                    }
                ),
            )
        except Exception as e:  # noqa: BLE001 —— 后台启动失败转错误结果
            return ToolResult(
                success=False, error=f"Failed to launch background task: {e}"
            )
