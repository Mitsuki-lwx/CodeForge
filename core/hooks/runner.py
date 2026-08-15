"""Hook 规则分发、四类动作、拦截判定、once/async/timeout。

拦截类事件（pre_tool）走 check_pre_tool 同步判定，其余事件走 run 分发。
所有 hook 失败只记日志，绝不抛出（N1）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from core.hooks.events import HookContext, context_to_env, context_to_stdin, field_value
from core.hooks.inject import InjectionStore
from core.hooks.rules import HookCondition, HookRule

logger = logging.getLogger(__name__)

# 事件 → 默认条件字段（if 省略 field 时使用）
DEFAULT_FIELDS: dict[str, str] = {
    "pre_tool": "tool_name",
    "pre_step": "iteration",
    "post_tool": "tool_name",
    "turn_start": "user_input",
    "turn_end": "user_input",
    "user_message": "content",
    "assistant_message": "content",
    "agent_error": "error_code",
    "context_compact": "trigger",
}


def _default_field(event: str, field: str) -> str:
    return field or DEFAULT_FIELDS.get(event, "")


def _match_rule(rule: HookRule, ctx: HookContext) -> bool:
    if rule.conditions is None:
        return True
    if rule.combinator == "all":
        return all(_match_atom(c, rule.event, ctx) for c in rule.conditions)
    if rule.combinator == "any":
        return any(_match_atom(c, rule.event, ctx) for c in rule.conditions)
    return False


def _match_atom(c: HookCondition, event: str, ctx: HookContext) -> bool:
    if c.matcher is None:
        return False
    value = field_value(ctx, _default_field(event, c.field))
    return c.matcher.match(value)


@dataclass
class _Outcome:
    blocked: bool = False
    reason: str = ""
    err: Exception | None = None


class HookRunner:
    """事件分派 + 动作执行。"""

    def __init__(
        self,
        rules: list[HookRule],
        cwd: str | Path,
        sources: list[str] | None = None,
        session_id: str = "main",
    ) -> None:
        self._rules = list(rules)
        self._sources = list(sources or [])
        self._cwd = Path(cwd)
        self._session_id = session_id
        self._once: set[str] = set()
        self._inject = InjectionStore()

    # ── 状态管理 ──────────────────────────────────────────────

    def reset(self) -> None:
        """/clear 与 reset_for_new_session 调用：清空 once 标记与注入文本。"""
        self._once.clear()
        self._inject.clear()

    def inject_store(self) -> InjectionStore:
        return self._inject

    @property
    def rules(self) -> list[HookRule]:
        return list(self._rules)

    @property
    def sources(self) -> list[str]:
        return list(self._sources)

    # ── 通用分发（非拦截事件）─────────────────────────────────

    async def run(self, event: str, ctx: HookContext) -> None:
        """非拦截事件分发：同步/后台执行命中规则，结果只记日志。"""
        for rule in self._rules:
            if rule.event != event:
                continue
            if rule.once and rule.name in self._once:
                continue
            if not _match_rule(rule, ctx):
                continue
            if rule.once:
                self._once.add(rule.name)  # 决定执行即标记，失败不重试
            if rule.async_run:
                asyncio.create_task(self._safe_exec(rule, ctx))
            else:
                await self._safe_exec(rule, ctx)

    async def _safe_exec(self, rule: HookRule, ctx: HookContext) -> None:
        try:
            await self._exec_action(rule, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— hook 失败只记日志，绝不中断主流程
            logger.warning("[hook %s] %s failed: %s", rule.name, rule.event, e)

    # ── 拦截判定（pre_tool）───────────────────────────────────

    async def check_pre_tool(self, ctx: HookContext) -> tuple[bool, str]:
        """同步串行执行命中规则；任一命令非 0 退出 → (True, stdout 原因)。"""
        for rule in self._rules:
            if rule.event != "pre_tool":
                continue
            if rule.once and rule.name in self._once:
                continue
            if not _match_rule(rule, ctx):
                continue
            if rule.async_run:
                continue  # 加载期已拒绝，防御性跳过
            if rule.once:
                self._once.add(rule.name)
            try:
                outcome = await self._exec_action(rule, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 生成子进程等失败，fail-open
                logger.warning("[hook %s] %s failed: %s", rule.name, "pre_tool", e)
                continue
            if outcome.err is not None:
                logger.warning(
                    "[hook %s] %s failed: %s", rule.name, "pre_tool", outcome.err
                )
                continue  # fail-open：hook 故障不误伤正常工具
            if outcome.blocked:
                return True, outcome.reason
        return False, ""

    # ── 拦截判定（pre_step：每轮 LLM 前，agent 循环级）───────────

    async def check_pre_step(self, ctx: HookContext) -> tuple[bool, str]:
        """同步串行执行 pre_step 规则；任一命令非 0 退出 → (True, 原因)。

        与 check_pre_tool 同契约（fail-open：hook 故障不误伤正常轮次）。
        """
        for rule in self._rules:
            if rule.event != "pre_step":
                continue
            if rule.once and rule.name in self._once:
                continue
            if not _match_rule(rule, ctx):
                continue
            if rule.async_run:
                continue
            if rule.once:
                self._once.add(rule.name)
            try:
                outcome = await self._exec_action(rule, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 失败 fail-open
                logger.warning("[hook %s] %s failed: %s", rule.name, "pre_step", e)
                continue
            if outcome.err is not None:
                logger.warning(
                    "[hook %s] %s failed: %s", rule.name, "pre_step", outcome.err
                )
                continue
            if outcome.blocked:
                return True, outcome.reason
        return False, ""

    # ── 动作执行 ──────────────────────────────────────────────

    async def _exec_action(self, rule: HookRule, ctx: HookContext) -> _Outcome:
        a = rule.action
        if a.type == "command":
            return await self._run_command(rule, ctx)
        if a.type == "prompt":
            self._inject.add(rule.name, a.content)
            return _Outcome()
        if a.type == "http":
            return await self._run_http(rule, ctx)
        if a.type == "subagent":
            print(
                f"[hook subagent] not yet implemented, skipped: {rule.name}",
                file=sys.stderr,
            )
            return _Outcome()
        return _Outcome(err=RuntimeError(f"unknown action type: {a.type}"))

    async def _run_command(self, rule: HookRule, ctx: HookContext) -> _Outcome:
        blocking = rule.event in ("pre_tool", "pre_step")
        proc = await asyncio.create_subprocess_shell(
            rule.action.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._cwd),
            env={**os.environ, **context_to_env(ctx)},
            creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
        )
        payload = context_to_stdin(ctx).encode()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(payload), timeout=rule.timeout
            )
        except TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001, S110 —— 超时清理，失败可忽略
                pass
            await proc.wait()
            return _Outcome(
                err=TimeoutError(f"hook command timed out after {rule.timeout}s")
            )
        code = proc.returncode or 0
        out = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        err_txt = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        if blocking and code != 0:
            # 拦截契约：非 0 退出 = 拦截，stdout（空则 stderr）作为拒绝原因
            return _Outcome(blocked=True, reason=(out or err_txt).strip())
        if code != 0:
            return _Outcome(err=RuntimeError(f"exit {code}: {err_txt.strip()}"))
        return _Outcome()

    async def _run_http(self, rule: HookRule, ctx: HookContext) -> _Outcome:
        import httpx

        ha = rule.action
        body = ha.body
        if body is None:
            body = json.dumps(asdict(ctx), sort_keys=True)
        else:
            try:
                body = ha.body.format_map(asdict(ctx))
            except (KeyError, IndexError, ValueError) as e:
                return _Outcome(err=RuntimeError(f"http body template failed: {e}"))
        try:
            async with httpx.AsyncClient(timeout=rule.timeout) as client:
                resp = await client.request(
                    ha.method, ha.url, content=body, headers=ha.headers
                )
        except httpx.HTTPError as e:
            return _Outcome(err=e)
        # 响应仅记日志，不回灌模型、不参与拦截（N6）
        logger.info(
            "hook http %s %s -> %s (body: %s)",
            ha.method,
            ha.url,
            resp.status_code,
            resp.text[:200],
        )
        return _Outcome()
