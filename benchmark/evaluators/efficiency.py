"""过程效率判定（硬性约束监控）。

把一次 item 的资源消耗归一化/比较到 `item['limits']` 硬上限：
  max_tool_calls  工具调用次数上限（缺省 40）
  max_seconds     单条耗时上限（缺省 120）
  max_tokens_out  输出 token 上限（缺省 4000）
任一项超限 → `over=True` 且 `efficiency` 分下降；超限还会在正确性侧降档
（调用方用 EfficiencyGATE 常量做乘法）。

数据从 `run_metrics`（engine 聚合：usage / tool_calls / elapsed_s）来。可观测性
关闭时这些字段缺省 → 该维度返回 value=None（缺测），**不抛错**。暴露原始值到
result metadata 供趋势/Diag 用。
"""

from __future__ import annotations

from typing import Any

# 超限时对正确性分的降档系数（供 runner 引用）
EFFICIENCY_GATE = 0.9

_DEFAULTS = {"max_tool_calls": 40, "max_seconds": 120, "max_tokens_out": 4000}


def _get_metrics(run_metrics: dict) -> dict[str, float]:
    usage = run_metrics.get("usage") or {}
    tok_out = usage.get("output_tokens")
    m = {
        "tool_calls": float(run_metrics.get("tool_calls")) if run_metrics.get("tool_calls") is not None else None,
        "elapsed_s": float(run_metrics.get("elapsed_s")) if run_metrics.get("elapsed_s") is not None else None,
        "tokens_out": float(tok_out) if tok_out is not None else None,
    }
    return m


def efficiency(item: dict, run_metrics: dict) -> dict:
    limits = dict(_DEFAULTS)
    limits.update((item.get("limits") or {}))
    actual = _get_metrics(run_metrics)

    over: list[str] = []
    if actual["tool_calls"] is not None and actual["tool_calls"] > limits["max_tool_calls"]:
        over.append(f"tool_calls {actual['tool_calls']:.0f}>{limits['max_tool_calls']}")
    if actual["elapsed_s"] is not None and actual["elapsed_s"] > limits["max_seconds"]:
        over.append(f"elapsed {actual['elapsed_s']:.1f}s>{limits['max_seconds']}")
    if actual["tokens_out"] is not None and actual["tokens_out"] > limits["max_tokens_out"]:
        over.append(f"tokens_out {actual['tokens_out']:.0f}>{limits['max_tokens_out']}")

    if all(v is None for v in actual.values()):
        return {"value": None, "over": False, "reasons": [], "raw": actual}

    # 归一：每个维度都在限内给满分；超一项降一档，下限 0
    score = 1.0 - 0.33 * len(over)
    score = max(0.0, score)
    return {"value": round(score, 3), "over": bool(over), "reasons": over, "raw": actual}


def evaluate_efficiency(item: dict, run_metrics: dict) -> dict:
    r = efficiency(item, run_metrics)
    over_extra = "；".join(r["reasons"])
    comment = f"超限: {over_extra}" if r["over"] else "资源在限内"
    # 把原始值并入 comment 便于裸 trace 里看
    raw = r["raw"]
    detail = ", ".join(f"{k}={v}" for k, v in raw.items() if v is not None)
    return {
        "name": "efficiency",
        "value": r["value"],
        "comment": f"{comment} ({detail})",
        "over": r["over"],
        "raw": raw,
    }
