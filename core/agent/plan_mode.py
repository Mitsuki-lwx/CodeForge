"""Plan Mode —— 计划模式开关 + 迭代感知提醒 + Plan 文件路径。

对齐 Mewcode prompts.py 中的 Plan Mode 提示语体系。
"""

from __future__ import annotations

import datetime
import random
from enum import Enum
from pathlib import Path

# ── 枚举 ──────────────────────────────────────────────────────


class PlanMode(str, Enum):
    OFF = "off"
    ON = "plan"


# ── 自动检测 ──────────────────────────────────────────────

_PLAN_INTENT_KEYWORDS: list[str] = [
    "先计划", "计划一下", "计划模式", "只规划", "先规划",
    "不要执行", "别执行", "别动手", "先别做",
    "只分析", "先分析", "分析一下", "只读模式",
    "先看看", "看看先", "先想想", "想一下",
    "给个方案", "先出方案", "出个计划",
    "别改", "不要改", "不要写", "先别改",
    "just plan", "plan first", "plan mode", "plan only",
    "don't execute", "don't run", "don't write", "do not execute",
    "read only", "read-only", "no write", "without executing",
    "let me plan", "give me a plan", "outline first",
]


def detect_plan_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _PLAN_INTENT_KEYWORDS)


# ── Plan 文件路径生成 ───────────────────────────────────────

_ADJECTIVES = [
    "bold", "bright", "calm", "cool", "deep", "fair", "fast", "fine",
    "glad", "keen", "kind", "lean", "mild", "neat", "pure", "safe",
    "slim", "soft", "tall", "warm", "wise", "grand", "swift", "vivid",
]
_NOUNS = [
    "sketch", "draft", "spark", "bloom", "trail", "ridge", "creek", "grove",
    "cliff", "cloud", "field", "forge", "frost", "haven", "pearl", "stone",
    "storm", "river", "tower", "delta", "flame", "orbit", "pulse", "shore",
]


def generate_plan_path(work_dir: str | Path = ".") -> Path:
    plans_dir = Path(work_dir) / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%m%d-%H%M")
    slug = f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{ts}"
    return plans_dir / f"{slug}.md"


# ── 迭代感知提醒（对齐 Mewcode）─────────────────────────────

_REMINDER_INTERVAL = 5

_PLAN_MODE_FULL_REMINDER = """\
Plan mode is active. The user indicated that they do not want you to execute yet -- you MUST NOT make any edits (with the exception of the plan file mentioned below), run any non-readonly tools (including changing configs or making commits), or otherwise make any changes to the system. This supercedes any other instructions you have received.

## Plan File Info:
{plan_file_info}
You should build your plan incrementally by writing to or editing this file. NOTE that this is the only file you are allowed to edit - other than this you are only allowed to take READ-ONLY actions.

## Plan Workflow

### Phase 1: Initial Understanding
Goal: Gain a comprehensive understanding of the user's request by reading through code and asking them questions.

1. Focus on understanding the user's request and the code associated with their request. Actively search for existing functions, utilities, and patterns that can be reused.
2. Use the Glob and Grep tools to explore the codebase.

### Phase 2: Design
Goal: Design an implementation approach. Read the critical files and design the changes needed.

### Phase 3: Review
Goal: Review the plan and ensure alignment with the user's intentions.
1. Read the critical files to deepen your understanding
2. Ensure that the plan aligns with the user's original request

### Phase 4: Final Plan
Goal: Write your final plan to the plan file (the only file you can edit).
- Begin with a Context section explaining why this change is being made
- Include only your recommended approach
- Include the paths of critical files to be modified
- Include a verification section describing how to test the changes

### Phase 5: Call ExitPlanMode
At the very end of your turn, call ExitPlanMode to indicate that you are done planning."""

_PLAN_MODE_SPARSE_REMINDER = (
    "Plan mode still active (see full instructions earlier in conversation). "
    "Read-only except plan file ({plan_path}). Follow 5-phase workflow."
)

_PLAN_MODE_EXIT_REMINDER = """\
## Exited Plan Mode

You have exited plan mode. You can now make edits, run tools, and take actions.{extra}"""

_PLAN_MODE_REENTRY_REMINDER = (
    "You have re-entered plan mode. Your previous plan file is at {plan_path}. "
    "Review it and continue from where you left off. You can update, refine, "
    "or restart the plan as needed. Follow the same 5-phase workflow as before."
)


def build_plan_mode_reminder(
    plan_path: str, plan_exists: bool, iteration: int
) -> str:
    """构建迭代感知的 Plan Mode system_reminder。

    - iteration 1: 完整提醒（5-phase workflow）
    - iteration 2-4: 稀疏提醒
    - 每 5 轮: 重复完整提醒
    """
    if plan_exists:
        plan_file_info = (
            f"Plan file: {plan_path}\n"
            f"A plan file already exists at {plan_path}. "
            "You can read it and make incremental edits using the Edit tool."
        )
    else:
        plan_file_info = (
            f"Plan file: {plan_path}\n"
            f"No plan file exists yet. You should create your plan at {plan_path} "
            "using the Write tool."
        )

    if iteration == 1:
        return _PLAN_MODE_FULL_REMINDER.format(plan_file_info=plan_file_info)

    attachment_index = (iteration - 1) // _REMINDER_INTERVAL
    if attachment_index % _REMINDER_INTERVAL == 0 and iteration > 1:
        return _PLAN_MODE_FULL_REMINDER.format(plan_file_info=plan_file_info)

    return _PLAN_MODE_SPARSE_REMINDER.format(plan_path=plan_path)


def build_plan_mode_exit_reminder(plan_path: str, plan_exists: bool) -> str:
    """退出 Plan Mode 时注入的提示。"""
    extra = ""
    if plan_exists:
        extra = f" The plan file is located at {plan_path} if you need to reference it."
    return _PLAN_MODE_EXIT_REMINDER.format(extra=extra)


def build_plan_mode_reentry_reminder(plan_path: str, plan_exists: bool) -> str:
    """重新进入 Plan Mode 时注入的提示。"""
    if not plan_exists:
        return ""
    return _PLAN_MODE_REENTRY_REMINDER.format(plan_path=plan_path)


# ── 兼容旧接口 ──────────────────────────────────────────────

def plan_system_reminder() -> str:
    """简单的单次 Plan Mode 提醒（兼容旧代码）。"""
    return _PLAN_MODE_SPARSE_REMINDER.format(plan_path="(plan file)")
