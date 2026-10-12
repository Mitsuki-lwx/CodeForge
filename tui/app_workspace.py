"""`CodeForgeApp` 的资源命令 —— `/worktree` 与 `/team`。

从 `tui/app.py` 抽出：两者都是"工作区级资源"的增删查（隔离 worktree、
队友团队），共用 `self.println` 与各自的管理器，与会话主循环解耦。

用 Mixin：`self.` 不变、调用点零改动、行为逐字不变。
"""

from __future__ import annotations


class WorkspaceCommandsMixin:
    """worktree 与 team 的管理命令。"""

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
