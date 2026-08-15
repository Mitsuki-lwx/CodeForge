"""Hook 规则数据结构与集中校验。

规则格式（YAML）：
  - name / event / if(all|any) / action / once / async / timeout
条件原子 = { field, op, value }，op ∈ exact | not | regex | glob。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.matcher import Matcher, compile_matcher

EVENT_NAMES = frozenset(
    {
        "session_start",
        "session_end",
        "turn_start",
        "turn_end",
        "user_message",
        "assistant_message",
        "pre_tool",
        "post_tool",
        "agent_error",
        "context_compact",
    }
)
ACTION_TYPES = frozenset({"command", "prompt", "http", "subagent"})
MATCH_OPS = frozenset({"exact", "not", "regex", "glob"})
INTERCEPT_EVENTS = frozenset({"pre_tool"})


@dataclass(slots=True)
class HookCondition:
    """单个原子条件：匹配 payload 的 field 路径。"""

    field: str
    op: str
    value: str
    matcher: Matcher | None = None  # 加载期编译一次、运行期复用（N11）


@dataclass(slots=True)
class HookAction:
    """动作对象：type 决定使用哪个子字段。"""

    type: str
    command: str = ""
    content: str = ""
    url: str = ""
    method: str = "POST"
    headers: dict = field(default_factory=dict)
    body: str | None = None  # 模板串，None 表示用 payload JSON
    prompt: str = ""


@dataclass(slots=True)
class HookRule:
    """一条 hook 规则：事件 + 条件 + 动作 + 执行控制。"""

    name: str
    event: str
    action: HookAction
    conditions: list[HookCondition] | None = None
    combinator: str | None = None  # "all" | "any"
    once: bool = False
    async_run: bool = False
    timeout: float = 5.0
    source: str = "project"  # 来源文件路径，/hooks 显示用


def validate_rule(rule: HookRule) -> list[str]:
    """校验单条规则，返回错误列表；空列表 = 通过。"""
    errors: list[str] = []
    if not rule.name:
        errors.append("name is required")
    if rule.event not in EVENT_NAMES:
        errors.append(f'unknown event "{rule.event}"')
    a = rule.action
    if a.type not in ACTION_TYPES:
        errors.append(f'unknown action type "{a.type}"')
    if a.type == "command" and not a.command:
        errors.append("command action requires command")
    if a.type == "prompt" and not a.content:
        errors.append("prompt action requires content")
    if a.type == "http":
        if not a.url:
            errors.append("http action requires url")
        if a.method not in ("GET", "POST"):
            errors.append(f'invalid http method "{a.method}"')
    if a.type == "subagent" and not a.prompt:
        errors.append("subagent action requires prompt")
    if rule.conditions is not None:
        if rule.combinator not in ("all", "any"):
            errors.append('if requires "all" or "any"')
        if not rule.conditions:
            errors.append("if requires at least one condition")
        for i, c in enumerate(rule.conditions):
            if not c.field:
                errors.append(f"condition #{i}: field is required")
            if c.op not in MATCH_OPS:
                errors.append(f"condition #{i}: unknown op {c.op!r}")
    if rule.event in INTERCEPT_EVENTS and rule.async_run:
        errors.append("async not allowed for blocking events")
    if rule.timeout <= 0:
        errors.append("timeout must be positive")
    return errors


def parse_rule(raw: dict, source: str, index: int) -> tuple[HookRule | None, list[str]]:
    """从 YAML 单条 dict 构造 HookRule；编译条件 matcher；非法返回 (None, errors)。"""
    name = str(raw.get("name") or "").strip()
    event = str(raw.get("event") or "").strip()

    raw_action = raw.get("action")
    if not isinstance(raw_action, dict):
        return None, ["missing action"]
    action = HookAction(
        type=str(raw_action.get("type") or ""),
        command=str(raw_action.get("command") or ""),
        content=str(raw_action.get("content") or ""),
        url=str(raw_action.get("url") or ""),
        method=str(raw_action.get("method") or "POST"),
        headers=dict(raw_action.get("headers") or {}),
        body=raw_action.get("body"),
        prompt=str(raw_action.get("prompt") or ""),
    )

    conditions = None
    combinator = None
    raw_if = raw.get("if")
    if raw_if is not None:
        if not isinstance(raw_if, dict):
            return None, ["if must be an object"]
        has_all = "all" in raw_if
        has_any = "any" in raw_if
        if has_all and has_any:
            return None, ['if cannot contain both "all" and "any"']
        key = "all" if has_all else "any" if has_any else None
        if key is None:
            return None, ['if requires "all" or "any"']
        combinator = key
        raw_atoms = raw_if.get(key)
        if not isinstance(raw_atoms, list) or not raw_atoms:
            return None, [f"{key} requires a non-empty list"]
        conditions = []
        for i, atom in enumerate(raw_atoms):
            if not isinstance(atom, dict):
                return None, [f"condition #{i} must be an object"]
            op = str(atom.get("op") or "")
            value = str(atom.get("value") or "")
            try:
                matcher = compile_matcher(op, value) if op else None
            except ValueError as e:
                return None, [f"condition #{i}: {e}"]
            conditions.append(
                HookCondition(
                    field=str(atom.get("field") or ""),
                    op=op,
                    value=value,
                    matcher=matcher,
                )
            )

    rule = HookRule(
        name=name,
        event=event,
        action=action,
        conditions=conditions,
        combinator=combinator,
        once=bool(raw.get("once", False)),
        async_run=bool(raw.get("async", False)),
        timeout=float(raw.get("timeout", 5.0)),
        source=source,
    )
    errors = validate_rule(rule)
    if errors:
        return None, errors
    return rule, []
