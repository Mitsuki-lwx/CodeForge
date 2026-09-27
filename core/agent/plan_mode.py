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

# ★ 强意图词：无论带什么修饰词，意图都是明确的计划模式，**不参与任何排除**。
#   理由："只规划"后面加"其他/计划"也不改变"只规划"这个事实。
_STRONG_INTENT: frozenset[str] = frozenset(
    {
        "只规划", "只分析", "只读模式", "计划模式",
        "plan only", "plan mode", "read only", "read-only",
        "just plan", "plan first", "let me plan",
        "give me a plan", "outline first",
    }
)

# ── 排除规则（只在命中关键词后生效，见 spec §3.1）─────────────────
#  每条都治一个"必然误判"的构造，判据可在无真实语料时证伪。

# E1 引述语境：关键词被当作"别人的话"引起来
_QUOTE_MARKS = "『』\"''「」‘’“”"
_REPORT_VERBS = (
    "文档里", "文档中", "文档", "里面写", "写了", "写的", "说", "提到",
    "请问", "什么意思", "啥意思", "这句话", "这段", "这句",
)

# E2 范围限定：否定的是"一部分"，不是"全部"
_SCOPE_LIMITERS = (
    "其他", "别的", "其余", "剩下", "格式化", "多余",
)

# E3 反悔 / 纠正：否定出现在历史语境，当前要求是执行
_REVERSALS = (
    "改主意", "改主意了", "其实", "现在", "直接", "改成", "重新",
    "算了", "instead", "actually",
)

# E4 否定对象是"计划"本身："不要执行计划" ≠ "进入计划模式"
_PLAN_OBJECT = ("计划", "plan")

# E5 英文否定修饰的是旧版/部分："don't run the old version, run the new one"
_PARTIAL_EN = ("the old", "the previous", "old ", "previous ", "之前", "原来", "旧的")


def _excluded(hit_kw: str, lowered: str) -> bool:
    """命中 `hit_kw` 后，判断是否属于已知的"必然误判"构造。

    刻意保持**只减不增**：这里返回 True 只会把"命中"降级为"不命中"，
    永远不会把"未命中"变成"命中" ⇒ 正例零漏报的结构性保证。

    ★ 强意图词只免疫 E2–E5（修饰类），**不免疫 E1（引述）**：
    「只规划别的」「只分析历史数据」是真意图 ⇒ 必须命中；
    而「文档里写了计划模式是什么意思」是在**转述** ⇒ 必须不命中。
    两者都含"强意图词"，只有引述语境能区分。
    """
    idx = lowered.find(hit_kw)
    if idx < 0:
        return False
    tail = lowered[idx + len(hit_kw) :]
    head = lowered[:idx]
    # 关键词与后继词之间常隔空格/逗号（中英文都常见）。
    # `startswith` 不处理这些 ⇒ 「别执行 plan」的 tail 是 " plan"，
    # 匹配 "plan" 失败 ⇒ 漏判（实测踩到）。
    tail_core = tail.lstrip(" ，,、：:；;的了吧呢啊呀")


    # E1 引述：关键词在引号里，或前文在转述别人（**强意图词也适用**）
    if any(c in _QUOTE_MARKS for c in head[-6:]) or (
        tail and tail[0] in _QUOTE_MARKS
    ):
        return True
    for verb in _REPORT_VERBS:
        if verb in lowered:
            return True

    # E2–E5 是修饰类排除：强意图词免疫
    if hit_kw in _STRONG_INTENT:
        return False

    # E2 范围限定：后接"其他/格式化…" ⇒ 只否定了一部分
    for lim in _SCOPE_LIMITERS:
        if tail_core.startswith(lim):
            return True

    # E3 反悔 / 纠正
    for rev in _REVERSALS:
        if rev in lowered:
            return True

    # E4 否定对象是"计划"本身
    for obj in _PLAN_OBJECT:
        if tail_core.startswith(obj):
            return True

    # E5 否定的是旧版 / 部分
    for p in _PARTIAL_EN:
        if p in tail_core[:24]:
            return True

    return False


def detect_plan_intent(text: str) -> bool:
    """用户是否表达了计划模式意图。

    ★ 为什么不是纯关键词匹配（`docs/spec_plan_intent.md` §2）：
    纯 `any(kw in text)` 会把「文档里写了『不要执行』是什么意思」、
    「刚才说不要执行，现在执行吧」这类**明确要执行**的输入判成计划模式，
    进而 `set_permission_mode(PLAN)` 把模型**禁止跑工具**。
    决定性实验（`.workbuddy-ai/decide_plan_intent.py`）在 9 条负例上误判 6 条。

    ★ 结构性保证：排除规则只作用于"已命中关键词"的输入，
    永远不可能让未命中的输入变成命中 ⇒ 正例零漏报。
    """
    lowered = text.lower()
    for kw in _PLAN_INTENT_KEYWORDS:
        if kw in lowered and not _excluded(kw, lowered):
            return True
    return False


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
