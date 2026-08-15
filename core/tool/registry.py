from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core.tool.context import ExecutionContext
from core.tool.errors import ToolError, ToolExecutionError, ToolNotFoundError, ToolValidationError
from core.tool.interface import Tool
from core.tool.result import ToolResult

logger = logging.getLogger(__name__)


def _resource_key(tool: Tool, input: dict) -> str:
    """Extract the resource key for concurrency locking (e.g. file path)."""
    for key in ("file_path", "path"):
        if key in input:
            return f"{tool.name()}:{input[key]}"
    return tool.name()


class ToolRegistry:
    """Central registry for tools with timeout, retry, and concurrency control."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration & lookup
    # ------------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """Register a tool. If a tool with the same name exists, it is replaced."""
        self._tools[tool.name()] = tool
        logger.debug("Registered tool: %s", tool.name())

    def get(self, name: str) -> Tool:
        """Look up a tool by name.

        Raises ``ToolNotFoundError`` if the name is unknown.
        """
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(name)

    def list(self) -> list[Tool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def count(self) -> int:
        """返回已注册工具数量（O(1)）。"""
        return len(self._tools)

    def definitions_filtered(self, allowed: list[str]) -> "ToolRegistry":
        """返回一个新 ToolRegistry，仅包含白名单中的工具 + 系统工具。

        allowed 为空时返回全部工具的拷贝。

        Args:
            allowed: 允许的工具名列表。

        Returns:
            新的 ToolRegistry 实例，仅含 allowed 中的工具和系统工具。

        Raises:
            SkillDependencyError: 白名单中某个工具名在 registry 中不存在。
        """
        from core.skills.errors import SkillDependencyError

        filtered = ToolRegistry()
        for tool in self._tools.values():
            if tool.is_system_tool:
                filtered.register(tool)
            elif not allowed or tool.name() in allowed:
                filtered.register(tool)

        # 检查白名单中是否有不存在的工具
        if allowed:
            for name in allowed:
                if name not in self._tools:
                    raise SkillDependencyError(
                        f"Tool '{name}' not found in registry"
                    )

        return filtered

    def system_definitions(self) -> list[Tool]:
        """返回所有系统工具（is_system_tool=True）的列表。"""
        return [t for t in self._tools.values() if t.is_system_tool]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        name: str,
        context: ExecutionContext,
        input: dict,
    ) -> ToolResult:
        """Execute a tool by name with timeout, retry, and concurrency control.

        Returns a ``ToolResult`` in all cases (never raises from execution).
        """
        try:
            tool = self.get(name)
        except ToolNotFoundError as e:
            return ToolResult(
                success=False,
                error=str(e),
                meta={"tool": name, "retries": 0},
            )

        # --- validate input ---
        error = tool.validate_input(input)
        if error is not None:
            return ToolResult(
                success=False,
                error=f"Validation failed: {error}",
                meta={"tool": name, "retries": 0},
            )

        # --- concurrency lock ---
        if not tool.is_concurrency_safe(input):
            key = _resource_key(tool, input)
            async with self._lock:
                if key not in self._locks:
                    self._locks[key] = asyncio.Lock()
            lock = self._locks[key]
        else:
            lock = _null_async_context()

        async with lock:
            # --- execute with retries ---
            last_error: Optional[Exception] = None
            timed_out = False
            attempts = 0
            max_attempts = 1 + tool.max_retries
            timeout = getattr(tool, "timeout_seconds", 30.0)

            while attempts < max_attempts:
                attempts += 1
                try:
                    result = await asyncio.wait_for(
                        tool.execute(context, input),
                        timeout=timeout,
                    )
                    if result.meta is None:
                        result.meta = {}
                    result.meta.setdefault("tool", name)
                    result.meta.setdefault("retries", attempts - 1)
                    result.meta.setdefault("timeout", timeout)
                    return result

                except asyncio.TimeoutError:
                    timed_out = True
                    last_error = ToolExecutionError(
                        name,
                        f"timed out after {timeout}s (attempt {attempts}/{max_attempts})",
                    )
                    logger.warning("%s (attempt %d/%d)", last_error, attempts, max_attempts)
                    if attempts < max_attempts:
                        wait = 1.0 * (2 ** (attempts - 1))  # 1s, 2s
                        await asyncio.sleep(wait)

                except ToolValidationError as e:
                    # Validation errors are not retried
                    return ToolResult(
                        success=False,
                        error=str(e),
                        meta={"tool": name, "retries": 0},
                    )

                except ToolError as e:
                    # Other known tool errors are not retried either
                    return ToolResult(
                        success=False,
                        error=str(e),
                        meta={"tool": name, "retries": attempts - 1},
                    )

                except Exception as e:
                    last_error = e
                    logger.error("Unexpected error executing tool '%s': %s", name, e)
                    if attempts < max_attempts:
                        wait = 1.0 * (2 ** (attempts - 1))
                        await asyncio.sleep(wait)

            # All retries exhausted
            return ToolResult(
                success=False,
                error=str(last_error) if last_error else "Unknown error",
                meta={
                    "tool": name,
                    "retries": attempts - 1,
                    "timeout": timeout,
                    "timed_out": timed_out,
                },
            )


class _null_async_context:
    """Reusable no-op async context manager for the concurrency-safe case."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        pass
