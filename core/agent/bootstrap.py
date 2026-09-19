"""会话装配 —— 唯一的构建入口。

**为什么需要这个模块**：新建会话（`tui.app::_run_async`）与恢复历史会话
（`CodeForgeApp.resume_session`）此前各自手写装配，导致两条路径的能力集
分叉——恢复出来的会话缺 hooks、缺会话状态工具、缺 skill catalog、
丢自定义 loop，且 `_dont_ask` 无人值守策略只在新会话生效。

现在两条路径都走 `build_session`，装配差异不再可能出现。调用方只负责
UI 与主循环。

约定：
- 本模块**不打印**任何东西，装配过程中的提示以 `SessionBundle.notices`
  返回，由调用方决定怎么展示。
- 恢复会话时传入 `session_dir` 与已还原的 `conversation`；此时沿用既有
  会话目录，不新建。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from config.protocol_defaults import effective_context_window
from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.agent.role_loader import load_catalog
from core.agent.runtime import SessionRuntime
from core.archive.writer import Writer, make_on_replace
from core.commands import Registry, register_builtins
from core.commands.skill_register import register_skills_as_commands
from core.context_compression.state import SessionContext, new_session_context
from core.hooks import HookRunner, load_hooks_config
from core.host.journal import SideEffectJournal
from core.host.lock import SessionLock
from core.instructions import load_instructions
from core.mcp import ConnectionPool, MCPToolAdapter, load_mcp_config
from core.notes import NoteStore, build_memory_index_text
from core.notes.state import SessionStateStore
from core.permissions.upgrade import ApprovalUpgrader
from core.skills import SkillExecutor, SkillLoader
from core.task.manager import BackgroundTaskManager
from core.task.tools import SendMessageTool, TaskGetTool, TaskListTool, TaskStopTool
from core.tool.context import ExecutionContext, ProgressSink
from core.tool.tools import get_default_registry
from core.tool.tools.agent_tool import AgentTool
from core.tool.tools.install_skill import InstallSkillTool
from core.tool.tools.load_skill import LoadSkillTool
from core.tool.tools.state_tool import register_state_tools
from core.worktree.manager import WorktreeManager
from llm.client import LLMClient

logger = logging.getLogger(__name__)

# 主 Agent 的执行身份 id。子 Agent 在此之上加 "-sub" 后缀
# （见 core/tool/tools/agent_tool.py），用于 span 归属。
MAIN_AGENT_ID = "main"


class CommandRegistrationError(RuntimeError):
    """命令注册中心冲突（N1：启动期致命，由调用方决定是否退出）。"""


@dataclass
class SessionBundle:
    """一次会话装配的全部产物。

    新建与恢复返回同一形状——调用方（TUI / headless / 未来的 host）
    不需要知道自己拿到的会话是新建的还是恢复的。
    """

    agent: Agent
    conversation: ConversationManager
    runtime: SessionRuntime
    writer: Writer
    journal: SideEffectJournal
    # 项目根目录。host 需要它来定位 run 库与 token 文件，且不能从 session_dir
    # 反推（那是 `<ws>/.codeforge/sessions/<id>`，靠 parents 索引太脆）。
    workspace: Path
    registry: object
    hook_runner: HookRunner
    cmd_registry: Registry
    skill_loader: SkillLoader
    skill_executor: SkillExecutor
    state_store: SessionStateStore
    task_mgr: BackgroundTaskManager
    wt_manager: WorktreeManager
    team_mgr: object
    subagent_catalog: object
    agent_tool: AgentTool
    notes: NoteStore
    instructions: str
    memory_text: str
    mcp_pool: ConnectionPool
    team_features: object | None = None
    # 会话级独占锁；`lock_session=True` 时非空，由 writer.close() 一并释放
    lock: SessionLock | None = None
    # 装配过程中的提示（hook 告警、skill 剔除、MCP 加载数、worktree 恢复数…）
    notices: list[str] = field(default_factory=list)

    def close(self) -> None:
        """关闭会话持有的文件句柄与锁（幂等）。"""
        # writer 持锁，故关 writer 即释放锁；`lock` 字段仅供调用方查询
        self.writer.close()
        self.journal.close()


def build_skill_catalog_text(loader: SkillLoader) -> str:
    """构建 Skill catalog 文本（名字 + 描述列表）。"""
    skills = loader.list_all()
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "",
        (
            "You have access to the following Skills. When a user request matches "
            "a Skill's description, call the `LoadSkill` tool with the skill name "
            "to activate it and receive the full SOP."
        ),
        "",
    ]
    for s in skills:
        lines.append(f"- **{s.meta.name}**: {s.meta.description}")
    return "\n".join(lines)


def _resolve_session_context(
    workspace: Path, session_dir: str | Path | None
) -> SessionContext:
    """决定本次装配用哪个会话目录。

    `session_dir` 为空 → 新建会话目录；否则沿用给定目录（恢复场景），
    并确保 spill 目录存在（Layer1 落盘需要）。
    """
    if session_dir is None:
        return new_session_context(str(workspace))
    d = Path(session_dir)
    spill = d / "tool-results"
    spill.mkdir(parents=True, exist_ok=True)
    return SessionContext(
        session_id=d.name, session_dir=str(d), spill_dir=str(spill)
    )


def _load_team_features(config_path: str, notices: list[str]) -> object | None:
    """读取 features 配置；失败不阻断启动，仅记录提示。"""
    try:
        from config.loader import load_config_full

        _, features = load_config_full(config_path)
        return features
    except Exception as e:  # noqa: BLE001 —— features 解析失败不阻断启动
        notices.append(f"features 解析失败: {e}")
        return None


async def _load_mcp_tools(
    registry, mcp_pool: ConnectionPool, notices: list[str]
) -> None:
    """加载 MCP 外部工具并注册进 registry；单个 server 失败不影响其他。"""
    mcp_configs = load_mcp_config()
    if not mcp_configs:
        return
    mcp_pool.configure(mcp_configs)
    server_count = 0
    tool_count = 0
    for cfg in mcp_configs:
        try:
            name = cfg["name"]
            client = await mcp_pool.get_client(name)
            if client:
                tools = await client.list_tools()
                for td in tools:
                    registry.register(MCPToolAdapter(client, td, name))
                    tool_count += 1
                server_count += 1
                notices.append(f"MCP: {name} → {len(tools)} tools loaded")
        except Exception:  # noqa: BLE001, S110 —— 单个 MCP server 失败不影响启动
            pass
    if server_count > 0:
        notices.append(f"MCP: {server_count} servers, {tool_count} tools total")


@dataclass
class ReuseContext:
    """同进程内切换会话时**复用**的长生命周期资源。

    `/resume` 需要重建 Agent，但下面这些资源不该跟着重建：

    - `mcp_pool`：重建会重新拉起 MCP 子进程；且旧池不关会泄漏子进程。
    - `task_mgr`：重建会让后台任务完成通知的消费协程失去订阅——
      `tui.app::_consume_task_done` 只在启动时 `subscribe_done()` 一次，
      换掉管理器后新会话的后台任务通知永远收不到。
    - `wt_manager`：重建会重复扫描并对 worktree 再跑一次 `recover_all()`。
    - `notes`：重建会丢掉已建的记忆索引。

    新建会话不传（全部新建）；恢复会话传入当前 app 持有的实例。
    注意 `team_mgr` **不在**此列——团队状态落盘（`_scan_and_recover`），
    重建管理器是安全的。
    """

    mcp_pool: ConnectionPool | None = None
    task_mgr: BackgroundTaskManager | None = None
    wt_manager: WorktreeManager | None = None
    notes: NoteStore | None = None


async def build_session(
    *,
    provider,
    workspace: str | Path,
    loop_spec: str = "",
    headless: bool = False,
    unattended_policy: str | None = None,
    session_dir: str | Path | None = None,
    conversation: ConversationManager | None = None,
    reuse: ReuseContext | None = None,
    lock_session: bool = False,
    config_path: str = "config.yaml",
    approval_upgrader: ApprovalUpgrader | None = None,
) -> SessionBundle:
    """装配一个可用的会话。

    Args:
        provider: 主模型 provider 配置。
        workspace: 项目根目录。
        loop_spec: Agent 循环策略（CLI `--loop` 优先，空则读 config `loop:`）。
        headless: 无头模式。当前等价于「无人值守放行 ask 级工具决策」，
            与既有 `--task` 行为保持一致。
        unattended_policy: 无人值守策略（`allow_all` / `allow_write` /
            `deny_all`）。给了它就走策略代答 `ask` 级决策；
            未给但 `headless=True` 时，退回历史的「一律放行」语义。
            两者都不给 = 有人值守（`ask` 走 HITL）。
        session_dir: 恢复会话时传入既有会话目录；为空则新建。
        conversation: 恢复会话时传入已还原的对话；为空则新建。
        reuse: 同进程内切换会话时复用的资源，见 `ReuseContext`。
        lock_session: 是否对 `session_dir` 加单写者独占锁。host 模式必须开——
            它要防止第二个进程往同一份 `conversation.jsonl` 里追加，导致恢复时
            读到两段互不衔接的对话。TUI 路径默认关（行为不变）。
            抢锁失败抛 `core.host.lock.SessionLockedError`。
        config_path: features 配置来源。
        approval_upgrader: 审批升级通道（`core.permissions.upgrade.ApprovalUpgrader`）。
            由**界面**建（消费者要在界面里弹窗），装进 `exec_ctx` 供下传——
            只有前台子 Agent 会被 `_run_foreground` 真正激活它，主 Agent 自己
            不挂（保持 `yield HITLRequired` 原路径）。传 `None` = 不启用。

    Returns:
        SessionBundle（含装配提示 notices）。
    """
    ws = Path(workspace)
    notices: list[str] = []
    reuse = reuse or ReuseContext()

    # 恢复会话必须成对提供：只给 conversation 会把既有对话写进新建的会话目录，
    # 造成"对话与目录错配"的静默数据问题。
    if conversation is not None and session_dir is None:
        raise ValueError(
            "传入 conversation 时必须同时指定 session_dir（恢复场景），"
            "否则既有对话会被写入新建会话目录"
        )

    # ── 项目指令 + 记忆索引（启动时加载一次）──
    instructions = load_instructions(str(ws))
    notes = reuse.notes if reuse.notes is not None else NoteStore(ws)
    memory_text = build_memory_index_text(notes)

    # ── 命令注册中心（启动期冲突即抛出，由调用方决定是否致命，N1）──
    cmd_reg = Registry()
    try:
        register_builtins(cmd_reg)
    except Exception as e:
        raise CommandRegistrationError(str(e)) from e

    # ── 核心运行时 ──
    client = LLMClient.create(provider)
    registry = get_default_registry()
    exec_ctx = ExecutionContext(
        cwd=ws,
        session_id=MAIN_AGENT_ID,
        # 进度汇：子 Agent 往里写「谁在做什么」，界面在长时间静默时来读，
        # 好把「仍在等待模型响应」这句（在跑子 Agent 时是误导）换成人话。
        # 挂在这里而不是 TUI 侧：装配是唯一入口，fork / 队友 / 嵌套子 Agent
        # 都从这里继承同一个汇，不必各自接线。
        progress=ProgressSink(),
        # 审批升级通道：与进度汇同理挂在这里（装配是唯一入口），但**语义不同**——
        # 它只是"可用"，并不自动对主 Agent 生效：主 Agent 保持 `yield HITLRequired`
        # 原路径，只有 `_run_foreground` 会把它下传并激活到子 Agent 实例上。
        approval_upgrader=approval_upgrader,
    )
    agent_config = AgentConfig(max_iterations=25)

    # ── Hook 系统（两级 YAML 加载，错误不阻断启动）──
    hook_rules, hook_problems, hook_sources = load_hooks_config(ws)
    hook_runner = HookRunner(
        rules=hook_rules,
        cwd=ws,
        sources=hook_sources,
        session_id=MAIN_AGENT_ID,
    )
    if hook_problems:
        notices.append(f"Hook: {'; '.join(hook_problems)}")

    # ── 会话上下文 + 压缩运行时 ──
    session_ctx = _resolve_session_context(ws, session_dir)
    runtime = SessionRuntime(
        session=session_ctx,
        context_window=effective_context_window(
            provider.protocol, provider.context_window
        ),
        notes=notes,
        hook_runner=hook_runner,
    )

    # ── 会话存档：JSONL 追加 + 压缩标记经 Conversation 回调驱动 ──
    # 单写者锁在 Writer 构造期抢、close() 释放：锁的生存期等于写入器的生存期。
    # 抢不到直接抛（SessionLockedError），调用方给可读提示——不静默双写。
    session_lock = SessionLock(session_ctx.session_dir) if lock_session else None
    writer = Writer(session_ctx.session_dir, model=provider.model, lock=session_lock)
    # 副作用 journal 与 conversation 同目录：两者靠 tool_use_id 配对做恢复判定
    journal = SideEffectJournal(session_ctx.session_dir)
    if conversation is None:
        conversation = ConversationManager(
            system_prompt="",
            on_append=writer.append,
            on_replace=make_on_replace(writer),
        )
    else:
        # 恢复场景：把既有对话的回调重绑到新 writer（旧 writer 由调用方关闭）
        conversation.set_callbacks(
            on_append=writer.append, on_replace=make_on_replace(writer)
        )

    agent = Agent(
        registry=registry,
        llm_client=client,
        exec_ctx=exec_ctx,
        conversation=conversation,
        config=agent_config,
        runtime=runtime,
        instructions=instructions,
        memory=memory_text,
        hooks=hook_runner,
        journal=journal,
    )

    # 无头模式：自动放行 ask 级工具，否则无人环境会卡在 HITL 审批上。
    # 注意 deny 不受影响（危险命令仍被拦）。
    #
    # 显式策略优先于 headless 这个粗粒度开关：host 需要有"放行到什么程度"的
    # 选择（默认只放只读），而不是只有"全放"和"全拒"两档。
    if unattended_policy is not None:
        agent.set_unattended_policy(unattended_policy)
    elif headless:
        agent._dont_ask = True

    # ── ExitPlanMode 回调注入 ──
    try:
        from core.tool.tools.exit_plan_mode import ExitPlanModeTool

        epm = registry.get("ExitPlanMode")
        if isinstance(epm, ExitPlanModeTool):
            epm._is_plan_mode = lambda: agent.plan_mode
            epm._plan_exists = lambda: bool(
                agent._plan_path and agent._plan_path.exists()
            )
    except Exception:  # noqa: BLE001, S110 —— 回调注入失败不影响启动
        pass

    # ── Skill 系统 ──
    skill_loader = SkillLoader(str(ws))
    skill_loader.load_all()

    load_skill_tool = LoadSkillTool()
    load_skill_tool.set_loader(skill_loader)
    load_skill_tool.set_agent(agent)
    registry.register(load_skill_tool)

    install_skill_tool = InstallSkillTool(catalog=skill_loader, work_dir=str(ws))
    registry.register(install_skill_tool)

    removed = skill_loader.validate_tools(registry)
    if removed:
        notices.append(
            f"Skill: {len(removed)} skill(s) removed due to missing tools: "
            f"{', '.join(removed)}"
        )

    # ── SubAgent / 后台任务 / Worktree ──
    subagent_catalog = load_catalog(str(ws))
    task_mgr = (
        reuse.task_mgr if reuse.task_mgr is not None else BackgroundTaskManager()
    )
    wt_manager = (
        reuse.wt_manager
        if reuse.wt_manager is not None
        else WorktreeManager(str(ws))
    )

    registry.register(TaskListTool(task_mgr))
    registry.register(TaskGetTool(task_mgr))
    registry.register(TaskStopTool(task_mgr))
    registry.register(SendMessageTool(task_mgr))

    agent_tool = AgentTool(
        catalog=subagent_catalog,
        task_mgr=task_mgr,
        parent_agent=None,  # 下方回填
        bg_enabled=True,
        wt_manager=wt_manager,
    )
    registry.register(agent_tool)

    # ── 会话状态（spec_session_state）：store + 工具 ──
    state_store = SessionStateStore(session_ctx.session_dir, notes=notes)
    register_state_tools(registry, state_store)
    agent.set_state_store(state_store)

    # ── Agent 循环策略（spec_loop）：CLI --loop 优先，否则 config loop: ──
    team_features = _load_team_features(config_path, notices)
    resolved_loop = loop_spec or (
        getattr(team_features, "loop", "") if team_features else ""
    )
    if resolved_loop:
        from core.agent.loop import load_loop

        agent.set_loop(load_loop(resolved_loop, agent))

    # ── Team 系统 ──
    from core.team.manager import Manager as TeamManager
    from core.team.registry import AgentNameRegistry
    from core.team.tools import SendMessageTool as TeamSendMessageTool
    from core.team.tools import TaskCreateTool, TaskUpdateTool
    from core.team.tools import TaskGetTool as TeamTaskGetTool
    from core.team.tools import TaskListTool as TeamTaskListTool

    name_reg = AgentNameRegistry()
    task_mgr.set_name_registry(name_reg)
    team_mgr = TeamManager(
        home_dir=str(Path.home()),
        wt_mgr=wt_manager,
        task_mgr=task_mgr,
        reg=name_reg,
    )

    registry.register(TaskCreateTool(team_mgr, ""))
    registry.register(TeamTaskGetTool(team_mgr, ""))
    registry.register(TeamTaskListTool(team_mgr, ""))
    registry.register(TaskUpdateTool(team_mgr, ""))
    registry.register(TeamSendMessageTool(team_mgr, "", "", ""))

    agent_tool.set_team_hook(team_mgr)
    task_mgr.on_task_done(lambda tid: team_mgr.handle_task_done(tid))

    # ── Coordinator Mode：收窄 Lead 工具集 + 注入纪律提示词 ──
    from core.coordinator import allowed_tools as coordinator_allowed_tools
    from core.coordinator import is_enabled as coordinator_enabled
    from core.coordinator import system_prompt_suffix as coordinator_prompt

    if team_features is not None and coordinator_enabled(team_features):
        agent.set_allowed_tools(coordinator_allowed_tools())
        agent.append_system_prompt(coordinator_prompt())
        notices.append(
            "Coordinator Mode 已启用（write_file/edit_file 已从工具集移除）"
        )

    # ── Skill 执行器 + catalog 注入 + 命令注册 ──
    skill_executor = SkillExecutor(
        catalog=skill_loader,
        runtime=runtime,
        registry=registry,
        provider=provider,
        workspace=str(ws),
    )
    skill_executor.set_agent(agent)
    agent.set_skill_catalog(build_skill_catalog_text(skill_loader))

    register_skills_as_commands(cmd_reg, skill_loader, skill_executor)
    install_skill_tool.set_on_installed(
        lambda _: register_skills_as_commands(cmd_reg, skill_loader, skill_executor)
    )

    from core.commands.builtin_skill import handle_skill
    from core.commands.types import Command
    from core.commands.types import Kind as CmdKind

    cmd_reg.register(
        Command(
            name="skill",
            description="管理 Skill（list / info / reload）",
            kind=CmdKind.LOCAL,
            handler=partial(
                handle_skill, catalog=skill_loader, executor=skill_executor
            ),
        )
    )

    # ── MCP 外部工具 ──
    # 复用传入的连接池（恢复场景），避免重复拉起 MCP 子进程
    mcp_pool = reuse.mcp_pool if reuse.mcp_pool is not None else ConnectionPool()
    await _load_mcp_tools(registry, mcp_pool, notices)

    # Agent 工具回填父 Agent 引用
    agent_tool.set_parent(agent)

    # ── Worktree 启动恢复：检测未退出的会话 ──
    # 仅新建管理器时扫描——复用时启动阶段已扫过，避免重复上报
    if reuse.wt_manager is None:
        recovered = wt_manager.recover_all()
        if recovered:
            notices.append(f"Worktree: 恢复 {len(recovered)} 个未退出的隔离会话")

    return SessionBundle(
        agent=agent,
        conversation=conversation,
        runtime=runtime,
        writer=writer,
        journal=journal,
        workspace=ws,
        registry=registry,
        hook_runner=hook_runner,
        cmd_registry=cmd_reg,
        skill_loader=skill_loader,
        skill_executor=skill_executor,
        state_store=state_store,
        task_mgr=task_mgr,
        wt_manager=wt_manager,
        team_mgr=team_mgr,
        subagent_catalog=subagent_catalog,
        agent_tool=agent_tool,
        notes=notes,
        instructions=instructions,
        memory_text=memory_text,
        mcp_pool=mcp_pool,
        team_features=team_features,
        lock=session_lock,
        notices=notices,
    )
