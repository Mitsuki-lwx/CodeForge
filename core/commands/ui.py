"""UI 抽象接口。

命令 handler 通过 UI 协议操作 TUI；CodeForgeApp 实现此协议。
NopUI 是测试桩，供 handler 单测使用。
"""

from __future__ import annotations

from typing import Protocol

from core.permissions.modes import PermissionMode


class UI(Protocol):
    """命令可调用的界面能力最小集。"""

    # ── 输出 ──
    def println(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...

    # ── 模式 ──
    def mode(self) -> PermissionMode: ...
    def set_mode(self, mode: PermissionMode) -> None: ...

    # ── 对话注入（PROMPT 类命令使用，触发 Agent 回合）──
    async def inject_and_send(self, label: str, preset_prompt: str) -> None: ...

    # ── 只读查询 ──
    def usage_in(self) -> int: ...
    def usage_out(self) -> int: ...
    def model_name(self) -> str: ...
    def cwd(self) -> str: ...
    def tool_count(self) -> int: ...
    def memory_files(self) -> list[str]: ...
    def session_path(self) -> str: ...
    def session_id(self) -> str: ...

    # ── 影响界面动作 ──
    def quit(self) -> None: ...
    async def force_compact(self) -> None: ...
    async def open_resume_menu(self) -> None: ...
    def clear_and_new_session(self) -> None: ...

    # ── 状态机查询 ──
    def idle(self) -> bool: ...

    # ── Skill 系统 ──
    def list_catalog_skills(self) -> list[dict]: ...
    def list_active_skills(self) -> list[str]: ...
    def clear_active_skills(self) -> None: ...
    async def append_assistant_message(self, text: str) -> None: ...


class NopUI:
    """测试桩：写入 no-op、查询返回零值。"""

    def println(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass

    def mode(self) -> PermissionMode:
        return PermissionMode.DEFAULT

    def set_mode(self, mode: PermissionMode) -> None:
        pass

    async def inject_and_send(self, label: str, preset_prompt: str) -> None:
        pass

    def usage_in(self) -> int:
        return 0

    def usage_out(self) -> int:
        return 0

    def model_name(self) -> str:
        return ""

    def cwd(self) -> str:
        return ""

    def tool_count(self) -> int:
        return 0

    def memory_files(self) -> list[str]:
        return []

    def session_path(self) -> str:
        return ""

    def session_id(self) -> str:
        return ""

    def quit(self) -> None:
        pass

    async def force_compact(self) -> None:
        pass

    async def open_resume_menu(self) -> None:
        pass

    def clear_and_new_session(self) -> None:
        pass

    def idle(self) -> bool:
        return True

    # ── Skill 系统 ──

    def list_catalog_skills(self) -> list[dict]:
        return []

    def list_active_skills(self) -> list[str]:
        return []

    def clear_active_skills(self) -> None:
        pass

    async def append_assistant_message(self, text: str) -> None:
        pass
