"""Agent 循环插件化（spec_loop）：AgentLoop 接口 + 默认 ReactLoop + 加载器。

对齐 deepseek-harness 的「agent-loop 为可替换插件」：默认 `ReactLoop` 封装现有
主/子 ReAct 逻辑（行为零变化）；用户可通过 config/CLI 指定自定义 loop 模块路径
（导出 `create_loop(agent)` 或 `loop` 实例）。加载失败回退 ReactLoop（优雅降级）。
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AgentLoop:
    """一个 agent 循环策略。

    实现者拿到完整 `agent` 实例（含 _registry 工具 / _client LLM / _runtime 压缩记忆 /
    _conversation），可自由决定每轮怎么调 LLM/工具、何时停止。

    - `run(agent, conv, user_input)`：主路径，产出事件流（AsyncGenerator[AgentEvent]，
      UI 依赖事件渲染）。
    - `run_to_completion(agent, conv, task="", events=None)`：子路径（fork/后台），
      返回最终文本。
    """

    def run(self, agent: Any, conv: Any, user_input: str):
        raise NotImplementedError  # pragma: no cover

    async def run_to_completion(
        self, agent: Any, conv: Any, task: str = "", events: Any = None
    ) -> str:
        raise NotImplementedError  # pragma: no cover


class ReactLoop(AgentLoop):
    """默认 ReAct 循环：委托现有逻辑，行为零变化。

    - `run` → 现有主循环 `agent._run_loop(user_input)`（事件流）。
    - `run_to_completion` → 现有子循环 `sub_agent._run_loop` + worktree 清理。
    """

    def run(self, agent: Any, conv: Any, user_input: str):
        return agent._run_loop(user_input)

    async def run_to_completion(
        self, agent: Any, conv: Any, task: str = "", events: Any = None
    ) -> str:
        from core.agent.sub_agent import _cleanup_worktree, _run_loop

        try:
            return await _run_loop(agent, conv, task, events)
        finally:
            _cleanup_worktree(agent)


def load_loop(name_or_path: str = "", agent: Any | None = None) -> AgentLoop:
    """按名字/路径加载 loop；失败回退 ReactLoop。

    Args:
        name_or_path: 空 / "react" → ReactLoop；否则当作自定义模块路径，importlib 加载。
        agent: 传给自定义 `create_loop(agent)`（可选，构造后注入）。

    Returns:
        AgentLoop 实例。任何加载失败 → ReactLoop（不抛、不阻断会话）。
    """
    if not name_or_path or name_or_path == "react":
        return ReactLoop()
    try:
        spec = importlib.util.spec_from_file_location(
            "codeforge_custom_loop", name_or_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法解析 loop 模块: {name_or_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "create_loop"):
            return mod.create_loop(agent)
        if hasattr(mod, "loop"):
            return mod.loop
        raise ImportError(
            f"loop 模块 {name_or_path} 缺少 create_loop() 或 loop 实例"
        )
    except Exception as e:  # noqa: BLE001 —— 加载失败回退，绝不阻断
        logger.warning("加载自定义 loop %r 失败，回退 ReactLoop: %s", name_or_path, e)
        return ReactLoop()


__all__ = ["AgentLoop", "ReactLoop", "load_loop"]
