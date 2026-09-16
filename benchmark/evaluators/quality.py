"""产物可维护性（AST 静态符号）判定。

从 agent 写进 cwd 的 `.py` 产物里提取**可判定、可复现**的符号（规范值见 checklist：
函数长 θ=50 行、圈复杂度 θ=10、裸数字字面量复现>1 次、非蛇形命名）。任一项命中即
扣分，返回归一化 0–1 + 命中明细。这是「用户拿过去要改」的可维护性代理指标。

只评 agent 新建/改写的文件：跳过 seed_files（否则把任务自带代码也算进去，失真）。
cwd 缺失或没有可分析文件时返回 None（缺测），不报错。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

# 规范门槛（常量便于调参）
MAX_FUNCTION_LINES = 50
MAX_CYCLOMATIC = 10
MAGIC_MIN_REUSE = 2  # 裸数字字面量被复用至少 2 次才算"魔法数"
SNAKE_RE = None  # 在 `_name_ok` 里用正则


def _import_snake():
    global SNAKE_RE
    import re
    if SNAKE_RE is None:
        SNAKE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
    return SNAKE_RE


def _agent_py_files(cwd: str | None, seed: dict) -> list[Path]:
    if not cwd:
        return []
    base = Path(cwd)
    if not base.is_dir():
        return []
    seed_names = set(seed.keys())
    out = []
    for p in sorted(base.rglob("*.py")):
        if p.name in seed_names or any(part in seed_names for part in p.parts):
            continue
        out.append(p)
    return out


def _cyclomatic(node: ast.AST) -> int:
    """圈复杂度：`if/elif/for/while/and/or` 计数 +1。（简化可不追 bool ops，稳为先）"""
    c = 1
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.While, ast.For)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
    return c


def _num_source_lines(body: list[ast.stmt]) -> int:
    if not body:
        return 0
    linenos = [getattr(n, "lineno", 0) for n in body if getattr(n, "lineno", 0)]
    if not linenos:
        return 0
    return max(linenos) - min(linenos) + 1


def quality(cwd: str | None, seed_files: dict) -> dict:
    """返回 {"value":0..1|None, "issues":[str,...]}（跳过 seed）。"""
    files = _agent_py_files(cwd, seed_files)
    if not files:
        return {"value": None, "issues": ["无可分析产物"]}

    issues: list[str] = []
    num_funcs = 0
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except (OSError, SyntaxError) as e:
            issues.append(f"{p.name}: 无法解析 ({type(e).__name__})")
            continue

        # magic number：跨整文件统计复用 > MAGIC_MIN_REUSE 的裸数值字面量
        literals: dict[Any, int] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                    and not isinstance(n.value, bool):
                literals[n.value] = literals.get(n.value, 0) + 1

        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            num_funcs += 1
            if not _import_snake().match(fn.name):
                issues.append(f"{p.name}:{fn.lineno} {fn.name} 非蛇形命名")
            if _num_source_lines(fn.body) > MAX_FUNCTION_LINES:
                issues.append(f"{p.name}:{fn.lineno} {fn.name} 超 {MAX_FUNCTION_LINES} 行")
            if _cyclomatic(fn) > MAX_CYCLOMATIC:
                issues.append(f"{p.name}:{fn.lineno} {fn.name} 圈复杂度 {_cyclomatic(fn)}>10")

        for lit, n in literals.items():
            if n > MAGIC_MIN_REUSE and not (isinstance(lit, int) and abs(lit) in (0, 1)):
                issues.append(f"{p.name}: 魔法数 {lit!r} 复用 {n} 次")

    distinct = sorted(set(issues))
    # 归一 0–1：每类问题 -0.2，封底 0
    score = max(0.0, 1.0 - 0.2 * len(distinct))
    return {"value": round(score, 3), "issues": distinct}


def evaluate_quality(item: dict, cwd: str | None = None) -> dict:
    seed = item.get("seed_files") or {}
    r = quality(cwd, seed)
    comment = ("；".join(r["issues"])) if r["issues"] else "产物整洁（无命中）"
    return {"name": "quality", "value": r["value"], "comment": comment or "未评测"}
