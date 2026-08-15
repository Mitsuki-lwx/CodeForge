"""团队 Manager —— Team 生命周期与成员花名册操作。

Manager 在单 CodeForge 进程内管理多个 Team；典型场景同时只有一个活跃 Team。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.team.backend.detect import detect_backend
from core.team.persistence import (
    atomic_write_json,
    read_json,
    reload_from_disk_locked,
)
from core.team.types import (
    MemberExistsError,
    MemberNotFoundError,
    Team,
    TeamError,
    TeamHasActiveMembersError,
    TeammateInfo,
    TeamNotFoundError,
)

logger = logging.getLogger(__name__)

# 团队根目录名（用户级 ~/.codeforge/teams/）。
TEAMS_DIR_NAME = "teams"


@dataclass
class Manager:
    """管理多个 Team。

    `teams` 按 sanitized_name 索引；`_lock` 只保护 `teams` dict。
    每个 Team 自带一把 `_lock` 保护其 members 变更（N4）。
    """

    teams: dict[str, Team] = field(default_factory=dict)
    home_dir: str = ""
    wt_mgr: object = None  # worktree.Manager
    task_mgr: object = None  # task.BackgroundTaskManager
    registry: object = None  # AgentNameRegistry

    _lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )

    def __init__(
        self,
        home_dir: str | Path = "",
        wt_mgr: object | None = None,
        task_mgr: object | None = None,
        reg: object | None = None,
    ) -> None:
        self.teams = {}
        self.home_dir = str(home_dir or Path.home())
        self.wt_mgr = wt_mgr
        self.task_mgr = task_mgr
        self.registry = reg
        self._lock = asyncio.Lock()
        self._scan_and_recover()

    # ── 目录 ───────────────────────────────────────────────────────

    def teams_root(self) -> Path:
        return Path(self.home_dir) / ".codeforge" / TEAMS_DIR_NAME

    def _config_path(self, sanitized: str) -> Path:
        return self.teams_root() / sanitized / "config.json"

    # ── 查询 ───────────────────────────────────────────────────────

    def get(self, sanitized_name: str) -> Team | None:
        """按 sanitized name 查询（自动再 sanitize 一次兜底）。"""
        from core.team.persistence import sanitize

        key = sanitize(sanitized_name) or sanitized_name
        return self.teams.get(key)

    def active_team(self) -> Team | None:
        """返回当前活跃团队（第一个；供未绑定 team 的全局协作工具解析）。"""
        teams = self.list_()
        return teams[0] if teams else None

    def list_(self) -> list[Team]:
        """按创建时间升序返回全部 Team。"""
        return sorted(self.teams.values(), key=lambda t: t.created_at)

    # ── 启动还原 ──────────────────────────────────────────────────

    def _scan_and_recover(self) -> None:
        """扫描 ~/.codeforge/teams/ 还原 teams dict。

        解析失败的目录 stderr 警告并跳过；不自动恢复 in-process 队员
        （进程重启后状态丢失）。pane 状态探测在本期函数外由 spawn 流程负责。
        """
        root = self.teams_root()
        if not root.exists():
            # 首次启动自动创建（N1）。
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning("cannot create teams dir %s: %s", root, e)
            return

        for child in root.iterdir():
            if not child.is_dir():
                continue
            cfg = child / "config.json"
            if not cfg.exists():
                continue
            try:
                team = Team.from_dict(read_json(cfg))
                self._fill_derived(team)
                self.teams[team.sanitized_name] = team
            except Exception as e:  # noqa: BLE001 —— 坏 config 跳过不中断
                logger.warning("skip malformed team dir %s: %s", child, e)

    def _fill_derived(self, team: Team) -> None:
        """按 home_dir 推导派生路径。"""
        cfg_dir = self.teams_root() / team.sanitized_name
        team.config_dir = str(cfg_dir)
        team.config_path = str(cfg_dir / "config.json")
        team.mailbox_dir = str(cfg_dir / "mailbox")
        team.tasks_path = str(cfg_dir / "tasks.json")

    # ── create ─────────────────────────────────────────────────────

    async def create(self, name: str, agent_type: str = "") -> Team:
        """创建 Team。同名冲突自动加 -2/-3 后缀。"""
        from core.team.persistence import sanitize

        sanitized = sanitize(name)
        if not sanitized:
            raise TeamNotFoundError(f"无法 sanitize 团队名: {name!r}")

        # 冲突后缀
        candidate = sanitized
        suffix = 2
        while candidate in self.teams:
            candidate = f"{sanitized}-{suffix}"
            suffix += 1

        backend = detect_backend()
        cfg_dir = self.teams_root() / candidate
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "mailbox").mkdir(exist_ok=True)

        lead = TeammateInfo(name="lead", agent_id="lead", is_active=None)
        team = Team(
            name=name,
            sanitized_name=candidate,
            lead_agent_id="lead",
            backend=backend,
            members=[lead],
        )
        self._fill_derived(team)
        atomic_write_json(team.config_path, team.to_dict())

        async with self._lock:
            self.teams[candidate] = team
        return team

    # ── delete ─────────────────────────────────────────────────────

    async def delete(self, name: str, force: bool = False) -> None:
        """删除 Team。

        非 force 且存在活跃成员（is_active != False）时抛 TeamHasActiveMembersError。
        force=True 时 kill 掉所有成员后端进程后清 worktree/session/config。
        """
        team = self.get(name)
        if team is None:
            raise TeamNotFoundError(f"未找到团队: {name!r}")

        async with team._lock:
            # F19c：先 reload disk，保证删除判断基于最新成员状态
            await reload_from_disk_locked(team)
            if not force:
                for m in team.members:
                    if m.is_active is not False:
                        raise TeamHasActiveMembersError(
                            f"成员 {m.name} 仍活跃，请先用 force 或等其空闲"
                        )

            # 非 lead 成员：kill 后端 + 清 worktree/session
            for m in list(team.members):
                if m.name == "lead":
                    continue
                await self._kill_member(m)
                await self._cleanup_member_resources(m)

            # 删 config 目录
            cfg_dir = Path(team.config_dir)
            if cfg_dir.exists():
                shutil.rmtree(cfg_dir, ignore_errors=True)

        async with self._lock:
            self.teams.pop(team.sanitized_name, None)

    async def _kill_member(self, member: TeammateInfo) -> None:
        """按成员 backend_type 杀掉后端进程（best-effort，失败仅警告）。"""
        if self.task_mgr is None:
            return
        try:
            from core.team.backend import new_backend

            backend = new_backend(member.backend_type, task_mgr=self.task_mgr)
            await backend.kill(member.pane_id, member.agent_id)
        except Exception as e:  # noqa: BLE001 —— 后端不存在/已死，仅警告
            logger.warning("kill member %s failed: %s", member.name, e)

    async def _cleanup_member_resources(self, member: TeammateInfo) -> None:
        """删会话目录与 worktree（best-effort，失败仅警告不中断）。"""
        if member.session_dir:
            shutil.rmtree(member.session_dir, ignore_errors=True)
        # worktree 名 = team-<sanitized>/<member>，由 wt_mgr.delete 处理变更保护
        name = getattr(member, "worktree_name", "")
        if name and self.wt_mgr is not None:
            try:
                await self.wt_mgr.delete(name)
            except Exception as e:  # noqa: BLE001
                logger.warning("worktree cleanup %s failed: %s", name, e)

    # ── Team 成员操作（T4，含 F19c reload 兜底）──────────────────

    async def add_member(self, team: Team, info: TeammateInfo) -> None:
        async with team._lock:
            await reload_from_disk_locked(team)
            if team.member_by_name(info.name) is not None:
                raise MemberExistsError(f"成员已存在: {info.name}")
            team.members.append(info)
            self._persist(team)

    async def set_member_active(self, team: Team, name: str, active: bool) -> None:
        async with team._lock:
            await reload_from_disk_locked(team)
            member = team.member_by_name(name)
            if member is None:
                raise MemberNotFoundError(f"成员不存在: {name}")
            member.is_active = active
            self._persist(team)

    async def remove_member(self, team: Team, name: str) -> None:
        async with team._lock:
            await reload_from_disk_locked(team)
            member = team.member_by_name(name)
            if member is None:
                raise MemberNotFoundError(f"成员不存在: {name}")
            team.members.remove(member)
            self._persist(team)

    def _persist(self, team: Team) -> None:
        atomic_write_json(team.config_path, team.to_dict())

    # ── spawn 入口（作为 TeamHook 提供给 Agent 工具）───────────────

    async def spawn_teammate(
        self,
        parent_agent: object | None = None,
        worktree_mgr: object | None = None,
        task_mgr: object | None = None,
        name_reg: object | None = None,
        team_name: str = "",
        member_name: str = "",
        prompt: str = "",
        subagent_type: str = "",
        model: str = "",
        **_: Any,
    ) -> str:
        """以 Manager 自身为参数的队员派生入口（供 AgentTool 的 team_hook 调用）。"""
        from core.team.spawn import spawn_teammate as _spawn

        return await _spawn(
            manager=self,
            parent_agent=parent_agent,
            worktree_mgr=worktree_mgr or self.wt_mgr,
            task_mgr=task_mgr or self.task_mgr,
            name_reg=name_reg or self.registry,
            team_name=team_name,
            member_name=member_name,
            prompt=prompt,
            subagent_type=subagent_type,
            model=model,
        )

    # ── 协作工具访问器（供 TF 具绑定 team 上下文）─────────────────

    def get_mailbox(self, team_name: str) -> object | None:
        """取某 Team 的邮箱 Box；Team 不存在返回 None。"""
        from core.team.mailbox import Box

        team = self.get(team_name)
        if team is None:
            return None
        return Box(team.mailbox_dir)

    def get_task_store(self, team_name: str) -> object | None:
        """取某 Team 的共享任务 Store；Team 不存在返回 None。"""
        from core.team.tasks import Store

        team = self.get(team_name)
        if team is None:
            return None
        return Store(team.tasks_path)

    def get_pane_id(self, agent_id: str) -> str | None:
        """反查某 agent_id 所属 Team 成员对应的 pane_id。"""
        for team in self._teams_in_memory():
            for m in team.members:
                if m.agent_id == agent_id:
                    return m.pane_id or None
        return None

    async def resolve_agent_id(self, name_or_id: str) -> str | None:
        """把队友名或 agent_id 统一解析为 agent_id（走名称注册表，缺省 agent_id 直查）。"""
        if self.registry is not None:
            try:
                resolved = self.registry.resolve(name_or_id)
                if resolved is not None:
                    return resolved
            except Exception as e:  # noqa: BLE001 —— 注册表异常兜底按 name 直查
                logger.debug("name registry resolve failed: %s", e)
        # 兜底：查所有 Team 成员按 name/agent_id 匹配
        for team in self._teams_in_memory():
            for m in team.members:
                if m.name == name_or_id:
                    return m.agent_id
                if m.agent_id == name_or_id:
                    return m.agent_id
        return None

    def _teams_in_memory(self) -> list[Team]:
        return list(self.teams.values())

    # ── T30: 队员 idle 通知（task.Manager.on_task_done 注册）─────────

    async def handle_task_done(self, agent_id: str) -> None:
        """Task 完成后：若 agent_id 是某 Team 成员 → 标空闲 + 通知 Lead。

        供 `task_mgr.on_task_done(lambda tid: team_mgr.handle_task_done(tid))` 注册。
        """
        member_team = self._find_member_team(agent_id)
        if member_team is None:
            return  # agent_id 不是任何 Team 成员 → 无需处理
        team, member = member_team
        name = member.name
        try:
            await self.set_member_active(team, name, False)
        except TeamError:
            pass  # 成员可能已被移除
        # 通知 Lead
        from core.team.mailbox import Box
        from core.team.mailbox.message import Message, MessageType

        mailbox = Box(team.mailbox_dir)
        try:
            await mailbox.write(
                team.lead_agent_id,
                Message(
                    from_=name,
                    to=team.lead_agent_id,
                    type=MessageType.TEXT,
                    summary=f"{name} idle",
                    content=f"agent {agent_id} finished work, available for new tasks",
                ),
            )
        except Exception as e:  # noqa: BLE001 —— 通知失败不阻断 idle 状态
            logger.warning("idle notify to lead failed for %s: %s", name, e)

    def _find_member_team(self, agent_id: str) -> tuple[Team, TeammateInfo] | None:
        for team in self._teams_in_memory():
            m = team.member_by_agent_id(agent_id)
            if m is not None:
                return team, m
        return None

    # ── T31: Lead mailbox 轮询（TUI watcher 消费）───────────────

    async def poll_lead_mailboxes(self) -> list[dict]:
        """轮询所有 Team 的 Lead 邮箱未读消息，标 read，返回消息列表。

        每条：{"team_name", "from_", "type", "summary", "content", "payload", "ts"}。
        content 截断上限 8000 字由 TUI watcher 侧处理。
        """
        from core.team.mailbox import Box

        out: list[dict] = []
        for team in self._teams_in_memory():
            box = Box(team.mailbox_dir)
            try:
                indices, msgs = await box.read_unread(team.lead_agent_id)
            except Exception as e:  # noqa: BLE001 —— 单个 team 读取失败跳过
                logger.warning("poll lead mailbox for %s failed: %s", team.name, e)
                continue
            if not msgs:
                continue
            for m in msgs:
                out.append({
                    "team_name": team.name,
                    "from_": m.from_,
                    "type": m.type.value,
                    "summary": m.summary,
                    "content": m.content,
                    "payload": m.payload,
                    "ts": m.timestamp,
                })
            await box.mark_read(team.lead_agent_id, indices)
        return out
