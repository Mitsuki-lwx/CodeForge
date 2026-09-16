"""（旧）单一 metric 判定器：基于 item 的 metric 对 CodeForge 输出打分。

迁移自原 `benchmark/evaluators.py`（因与 `benchmark/evaluators/` 子包同名冲突而迁入本包）。
统一入口 `score_item(item, output, cwd) -> dict`，返回 Langfuse Evaluation 兼容的
dict（name/value/comment），可直接喂给 `client.create_score`。

支持 metric：
  contains      expected_output（或 item['regex']）是否出现在输出里
  exact         规范化（strip / case-fold）后相等
  regex          item['regex']（或 expected_output）正则匹配输出
  pytest_pass   在 temp cwd 里跑 `pytest -q`；全部通过得 1.0，否则 0.0

pytest_pass 需要真正执行：依赖 cwd 里已有被 agent 写好的文件，因此调用方需在
engine 清理临时目录之前评分（或把 tmpdir 传进来）。

保留在包内 re-export（`benchmark.evaluators.score_item` / `.VALID_METRICS`），
与新增的多维评测并存、向后兼容。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any


VALID_METRICS = {"contains", "exact", "regex", "pytest_pass"}


def _norm(text: str) -> str:
    """规范化用于比较的文本：去首尾空白、小写、合并多余空白。"""
    return " ".join(text.strip().lower().split())


def _match_target(item: dict) -> str:
    """返回匹配用的文案：regex 优先用 item['regex']，否则 expected_output。"""
    if item.get("metric") == "regex":
        return item.get("regex") or item.get("expected_output", "")
    return item.get("expected_output", "")


def _contains(item: dict, output: str) -> float:
    target = _match_target(item)
    if not target:
        return 0.0
    return 1.0 if target in output else 0.0


def _exact(item: dict, output: str) -> float:
    target = item.get("expected_output", "")
    if not target:
        return 0.0
    return 1.0 if _norm(target) == _norm(output) else 0.0


def _regex(item: dict, output: str) -> float:
    pattern = _match_target(item)
    if not pattern:
        return 0.0
    try:
        return 1.0 if re.search(pattern, output) else 0.0
    except re.error:
        return 0.0


def _pytest_pass(item: dict, output: str, cwd: Path | None) -> float:
    if cwd is None:
        return 0.0
    try:
        # 用项目 venv 的 python 跑 pytest，避免系统 pytest 缺失
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=120,
        )
        # pytest -q 成功时 rc=0
        return 1.0 if proc.returncode == 0 else 0.0
    except Exception:  # noqa: BLE001 —— 评测失败计 0 分，绝不打断
        return 0.0


def score_item(item: dict, output: str, cwd: Path | None = None) -> dict[str, Any]:
    """按 metric 打分，返回 Langfuse Evaluation 兼容 dict。

    Returns:
        {"name": <metric>, "value": 0.0|1.0, "comment": <说明>}
    """
    metric = item.get("metric", "contains")
    if metric not in VALID_METRICS:
        return {"name": metric, "value": 0.0, "comment": f"未知 metric: {metric}"}

    if metric == "contains":
        v = _contains(item, output)
    elif metric == "exact":
        v = _exact(item, output)
    elif metric == "regex":
        v = _regex(item, output)
    elif metric == "pytest_pass":
        v = _pytest_pass(item, output, cwd=cwd)
    else:
        v = 0.0

    comment = ""
    if metric == "pytest_pass":
        comment = "pytest 全通过" if v else "pytest 有失败或未运行"
    elif metric == "contains":
        comment = f"命中 expected_output={_match_target(item)!r}" if v else f"未命中 {_match_target(item)!r}"
    elif metric == "exact":
        comment = "完全一致" if v else "不一致"
    elif metric == "regex":
        comment = f"命中正则 {_match_target(item)!r}" if v else f"未命中正则 {_match_target(item)!r}"

    return {"name": metric, "value": v, "comment": comment}
