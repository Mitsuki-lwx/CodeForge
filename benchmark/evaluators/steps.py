"""步骤×权重评分器 —— 把一个任务拆成多个可观测步骤，各步给连续分，再按权重聚合。

对标 `legacy.score_item` 的 0/1 一刀切：本模块让任务能声明 `item["steps"]`，
每一 `step` 是一个 {name, check, weight, ...}，`check` 从封闭动词集 `VALID_STEP_CHECKS`
选一个，返回 0..1（能连续就给连续分，如 contains 的命中比例），各步按权重加权得总分。
硬门 `step_gates` / 单步 `hard:True` 不过时总分折成 0（`HARD_GATE_DISCOUNT=0.0`，
与 `efficiency.EFFICIENCY_GATE` 的「打折而非无视」同思路）。

契约（复用既有，不发明）：
  - `benchmark.evaluators.legacy._pytest_pass`  跑 cwd 里 pytest，rc==0 → 1.0
  - `benchmark.evaluators.semantic.run_exe`     在 cwd 跑命令 → (rc, stdout, stderr)
  - `benchmark.evaluators.semantic.semantic_equiv`  语义归约比较 → (0|1, note)
  - `benchmark.evaluators.semantic.evaluate_semantic` 的 `cases` 通过率逻辑照搬

安全：`item["steps"]` 缺失时返回 value=None（no-op，不进 `evaluate_multi` 的 4 键协议）；
每个 sub-check 用 try/except 兜底返回 0.0/None 注释，绝不抛（效仿 legacy._pytest_pass）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from benchmark.evaluators import legacy, semantic

# 封闭动词集：新增 step 类型只需在此登记 + 加一个 `_check_<verb>` 分支。
VALID_STEP_CHECKS: frozenset[str] = frozenset(
    {
        "contains",  # output 命中若干子串，连续 = 命中比例
        "file_exists",  # cwd 下某文件存在
        "file_contains",  # cwd 下某文件含若干锚点行，连续 = 锚点命中比例
        "regex",  # output 匹配正则
        "exact",  # output 规范化后与 want 相等
        "negates",  # output 不含若干禁用子串，连续 = 1 - 命中比例
        "pytest_pass",  # cwd 里 pytest 全过
        "runs",  # cwd 里跑 cmd/script，rc==0
        "cases_ratio",  # 镜像 evaluate_semantic 的 cases 通过率
    }
)

HARD_GATE_DISCOUNT = 0.0  # 硬门不过 → 总分 × 0.0（归零）


def evaluate_steps(
    item: dict, output: str, run_metrics: dict, cwd: str | None = None
) -> dict[str, Any]:
    """对一条 item 做步骤×权重打分。

    Args:
        item: 数据集 item；可选含 `steps`（列表）与 `step_gates`（列表）。
        run_metrics: `run_one` 返回的 `{usage, tool_calls, elapsed_s}` 等（本实现暂不直接用）。
        cwd: agent 产物临时目录；pytest_pass/runs/cases_ratio/file_* 需要。
        output: agent 最终文本。

    Returns:
        {"name":"steps","value":0..1|None,"comment","steps":[per-step dict],
         "gated","gates_failed"}。无 `steps` → value=None（no-op）。
    """
    steps = item.get("steps")
    if not steps:
        return {
            "name": "steps",
            "value": None,
            "comment": "无 steps（缺测）",
            "steps": [],
            "gated": False,
            "gates_failed": False,
        }

    scored: list[dict[str, Any]] = []
    for s in steps:
        try:
            value, note = _run_step(s, output, cwd)
        except Exception:  # noqa: BLE001 —— 单步失败计 0 分，绝不抛
            value, note = 0.0, "step 执行异常"
        scored.append(
            {
                "name": s.get("name", "?"),
                "check": s.get("check", "?"),
                "weight": float(s.get("weight", 1) or 0),
                "value": value,
                "comment": note,
            }
        )

    total_w = sum(s["weight"] for s in scored)
    if total_w <= 0:
        total = None
        comment = "step 权重之和为 0，无法聚合"
    else:
        total = round(sum(s["value"] * s["weight"] for s in scored) / total_w, 3)
        comment = f"{_pass_count(scored)}/{len(scored)} 步达标（加权 {total}）"

    # 硬门判定：step_gates 命中 + 单步 hard 标记
    gate_failed, gate_notes = _eval_gates(item, scored)
    if gate_failed:
        total = round(total * HARD_GATE_DISCOUNT, 3) if total is not None else None
        comment += f"；硬门禁[{','.join(gate_notes) or 'hard'}]未过 → {total}"

    return {
        "name": "steps",
        "value": total,
        "comment": comment,
        "steps": scored,
        "gated": bool(item.get("step_gates")) or any(s.get("hard") for s in steps),
        "gates_failed": gate_failed,
    }


def _run_step(step: dict, output: str, cwd: str | None) -> tuple[float, str]:
    """执行单个 step，返回 (score 0..1, comment)。"""
    check = step.get("check", "")
    if check not in VALID_STEP_CHECKS:
        return 0.0, f"未知 check: {check!r}"
    if check == "contains":
        return _check_contains(output, step.get("contains") or [])
    if check == "file_exists":
        return _check_file_exists(cwd, step.get("path", ""))
    if check == "file_contains":
        return _check_file_contains(
            cwd, step.get("path", ""), step.get("needles") or []
        )
    if check == "regex":
        return _check_regex(output, step.get("regex", ""))
    if check == "exact":
        return _check_exact(output, step.get("want", ""))
    if check == "negates":
        return _check_negates(output, step.get("forbidden") or [])
    if check == "pytest_pass":
        return _check_pytest(cwd)
    if check == "runs":
        return _check_runs(cwd, step.get("cmd"), step.get("script"))
    if check == "cases_ratio":
        return _check_cases(step, cwd)
    return 0.0, f"未实现 check: {check}"  # pragma: no cover


def _check_contains(output: str, needles: list[str]) -> tuple[float, str]:
    if not needles:
        return 0.0, "contains 缺 needles"
    hits = sum(1 for n in needles if n in output)
    return (hits / len(needles)), f"命中 {hits}/{len(needles)} 子串"


def _check_file_exists(cwd: str | None, path: str) -> tuple[float, str]:
    if cwd is None:
        return 0.0, "缺 cwd"
    p = Path(cwd) / path
    ok = p.is_file()
    return (1.0 if ok else 0.0), (f"文件存在 {path}" if ok else f"文件缺失 {path}")


def _check_file_contains(
    cwd: str | None, path: str, needles: list[str]
) -> tuple[float, str]:
    if cwd is None:
        return 0.0, "缺 cwd"
    if not needles:
        return 0.0, "file_contains 缺 needles"
    p = Path(cwd) / path
    if not p.is_file():
        return 0.0, f"文件缺失 {path}"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return 0.0, f"读取失败 {path}"
    hits = sum(1 for n in needles if n in text)
    return (hits / len(needles)), f"{path} 锚点命中 {hits}/{len(needles)}"


def _check_regex(output: str, pattern: str) -> tuple[float, str]:
    import re

    if not pattern:
        return 0.0, "regex 缺 pattern"
    try:
        ok = re.search(pattern, output) is not None
    except re.error:
        return 0.0, "regex 非法"
    return (1.0 if ok else 0.0), ("正则命中" if ok else f"正则未命中 {pattern!r}")


def _check_exact(output: str, want: str) -> tuple[float, str]:
    legacy_norm = lambda t: " ".join(t.strip().lower().split())
    ok = legacy_norm(output) == legacy_norm(want)
    return (1.0 if ok else 0.0), ("完全一致" if ok else "不一致")


def _check_negates(output: str, forbidden: list[str]) -> tuple[float, str]:
    if not forbidden:
        return 0.0, "negates 缺 forbidden"
    hits = sum(1 for f in forbidden if f in output)
    score = 0.0 if hits else 1.0  # 任一禁用子串出现即 0
    return score, ("无禁用子串" if hits == 0 else f"含禁用子串 {hits} 个")


def _check_pytest(cwd: str | None) -> tuple[float, str]:
    if cwd is None:
        return 0.0, "缺 cwd（pytest 需产物目录）"
    v = legacy._pytest_pass({}, "", Path(cwd))
    return (1.0 if v else 0.0), ("pytest 全通过" if v else "pytest 有失败或未运行")


def _check_runs(
    cwd: str | None, cmd: list[str] | None, script: str | None
) -> tuple[float, str]:
    if cwd is None:
        return 0.0, "缺 cwd"
    if script is not None:
        run_cmd = [sys.executable, "-c", script]
    elif cmd:
        run_cmd = list(cmd)
    else:
        return 0.0, "runs 缺 cmd/script"
    try:
        rc, _out, _err = semantic.run_exe(cwd, cmd=run_cmd, input_text="")
    except Exception:  # noqa: BLE001
        return 0.0, "runs 执行异常"
    return (1.0 if rc == 0 else 0.0), (
        "rc=0 运行成功" if rc == 0 else f"rc≠0 运行失败 ({rc})"
    )


def _check_cases(step: dict, cwd: str | None) -> tuple[float, str]:
    """镜像 evaluate_semantic 的 cases 通过率（把 step 当 mini item 用）。"""
    mini = {
        "cases": step.get("cases"),
        "cmd": step.get("cmd"),
        "script": step.get("script"),
        "reference": step.get("reference"),
        "kind": step.get("kind", "lines"),
    }
    res = semantic.evaluate_semantic(mini, "", cwd)
    v = res.get("value")
    return ((v if v is not None else 0.0), res.get("comment", ""))


def _eval_gates(item: dict, scored: list[dict]) -> tuple[bool, list[str]]:
    """判定硬门是否失败。返回 (failed, [未过的门名])。

    规则：
      - `item["step_gates"]` 每条 {when, thresh=1.0}：when 指向某个 step 名，该 step score<thresh → fail。
      - 单步 `hard:True`：该 step score<1.0 → fail。
    引用缺失的 step 名视为不过（仅告警，绝不抛）。
    """
    failed = False
    notes: list[str] = []

    for g in item.get("step_gates") or []:
        when = g.get("when", "")
        thresh = float(g.get("thresh", 1.0))
        st = _lookup_step(scored, when)
        if st is None:
            continue
        if st["value"] is not None and st["value"] < thresh:
            failed = True
            notes.append(st["name"])

    # 单步 hard 标记：score<1.0 即失败
    for st in scored:
        if st["value"] is not None and st["value"] < 1.0:
            hard_steps = _hard_step_names(item, st["name"])
            if hard_steps:
                failed = True
                notes.append(st["name"])
                break  # 记一个门即够催「硬门禁」，避免重复

    return failed, notes


def _hard_step_names(item: dict, name: str) -> bool:
    """某 step 是否被标记 hard（`hard:True`）。"""
    for s in item.get("steps") or []:
        if s.get("name") == name and s.get("hard"):
            return True
    return False


def _lookup_step(scored: list[dict], name: str) -> dict | None:
    for st in scored:
        if st["name"] == name:
            return st
    return None


def _pass_count(scored: list[dict]) -> int:
    return sum(1 for s in scored if s["value"] is not None and s["value"] >= 1.0)


__all__ = ["HARD_GATE_DISCOUNT", "VALID_STEP_CHECKS", "evaluate_steps"]
