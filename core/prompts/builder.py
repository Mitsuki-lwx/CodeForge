"""提示拼装器。

将模块化系统提示、工具定义、环境信息拼装成 PromptAssembly，
区分可缓存块（stable system modules + tool defs）与不缓存块（env info）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.prompts.modules import PromptModule, get_all_modules


@dataclass
class CachedBlock:
    """可缓存的内容块。"""

    content: str
    cache_control: bool = True


@dataclass
class UncachedBlock:
    """不缓存的内容块。"""

    content: str


@dataclass
class PromptAssembly:
    """拼装完成的系统提示，分成缓存/不缓存两组。"""

    cached: list[CachedBlock] = field(default_factory=list)
    uncached: list[UncachedBlock] = field(default_factory=list)


class PromptBuilder:
    """系统提示拼装器。

    职责：
    - 模块按 priority 排序 → 拼成 stable_block
    - 工具定义为独立 stable 块
    - 环境信息为 uncached 块
    - 保证 cached_blocks 跨轮逐字节一致
    """

    def __init__(
        self,
        model: str = "",
        version: str = "0.1.0",
        instructions: str = "",
        memory: str = "",
    ) -> None:
        self._model = model
        self._version = version
        self._modules: list[PromptModule] = get_all_modules()
        # 排序保证跨轮一致：priority → name（打破平局）
        self._modules.sort(key=lambda m: (m.priority, m.name))
        # 注入文本（自定义指令 / 长期记忆索引），空则跳过
        self._instructions = instructions
        self._memory = memory

    # ── Public API ──────────────────────────────────────────────────

    def set_injections(self, instructions: str = "", memory: str = "") -> None:
        """设置自定义指令与长期记忆索引的注入文本（F43）。"""
        self._instructions = instructions
        self._memory = memory

    def set_modules(self, modules: list[PromptModule]) -> None:
        """替换模块列表（用于测试或自定义模块）。"""
        self._modules = sorted(modules, key=lambda m: (m.priority, m.name))

    def build_assembly(self, env_info: str = "") -> PromptAssembly:
        """拼装完整系统提示。

        Args:
            env_info: 环境信息文本（来自 collect_environment()）

        Returns:
            PromptAssembly，cached 块与 uncached 块分离
        """
        # 稳定系统模块（跳过空内容）
        module_parts: list[str] = []
        for m in self._modules:
            if m.content.strip():
                module_parts.append(m.content.strip())

        # 注入文本接在固定模块之后（custom_instructions → long_term_memory 次序）
        parts = list(module_parts)
        if self._instructions.strip():
            parts.append(self._instructions.strip())
        if self._memory.strip():
            parts.append(self._memory.strip())

        stable_text = "\n\n".join(parts)

        assembly = PromptAssembly()
        assembly.cached.append(CachedBlock(content=stable_text))

        if env_info.strip():
            assembly.uncached.append(UncachedBlock(content=env_info.strip()))

        return assembly

    def build_tool_defs_block(self, tool_defs: list[dict]) -> CachedBlock:
        """将工具定义序列化为可缓存块。

        序列化格式固定以保证跨轮逐字节一致：
        - 按 name 排序
        - JSON 用 sorted keys
        """
        import json

        # 按 name 排序，保证跨轮顺序固定
        sorted_defs = sorted(tool_defs, key=lambda t: t.get("name", ""))
        text = json.dumps(sorted_defs, ensure_ascii=False, sort_keys=True, indent=2)
        return CachedBlock(content=text)

    # ── Convenience ──────────────────────────────────────────────────

    def build_full(
        self, env_info: str, tool_defs: list[dict]
    ) -> PromptAssembly:
        """便捷方法：一次完成系统提示 + 工具定义的拼装。

        这是 Agent 调用的主入口。
        """
        assembly = self.build_assembly(env_info)

        # 工具定义作为第二个 cached block
        tool_block = self.build_tool_defs_block(tool_defs)
        assembly.cached.append(tool_block)

        return assembly
