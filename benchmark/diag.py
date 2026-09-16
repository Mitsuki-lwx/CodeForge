"""读本地 JSONL 观测文件,产出「瓶颈层级」诊断报告。

读 `~/.codeforge/obs/traces/data.jsonl`(由 core.observability 默认落盘,配置配了
OTLP 时也同时写本地)。按 trace_id 分组,对每个 `bench:*` 根 trace 计算:

  工具层(工具瓶颈)
    - tool.* span 数量、合计耗时(duration_ms)、avg
    - retries>0 / timed_out / status==ERROR(失败)计数
  循环层(循环瓶颈)
    - chat.completions 的 codeforge.iteration 最大值(= 迭代数)
    - 每迭代 codeforge.iteration_context_chars(初值→终值=上下文膨胀)
    - 每迭代 gen_ai.usage.output_tokens(初值/终值)
  多智能体
    - 单 agent run:工具 vs 循环耗时占比;多 agent 留待 coordinator 级比较

用法:
  python -m benchmark.diag [--obs-dir ~/.codeforge/obs] [--trace <trace_id>]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

_DEFAULT_OBS = Path.home() / ".codeforge" / "obs"


def _load_spans(obs_dir: Path) -> list[dict[str, Any]]:
    p = obs_dir / "traces" / "data.jsonl"
    if not p.exists():
        return []
    spans = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            spans.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return spans


def _group_by_trace(spans: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        tid = s.get("trace_id")
        if tid:
            groups.setdefault(str(tid), []).append(s)
    return groups


def _ms(s: dict[str, Any]) -> float:
    st = s.get("start_time_unix_nano")
    en = s.get("end_time_unix_nano")
    if st is None or en is None:
        return 0.0
    return (en - st) / 1e6


def _bench_root(span: dict[str, Any]) -> bool:
    return str(span.get("name", "")).startswith("bench:")


def analyze_trace(spans: list[dict[str, Any]]) -> dict[str, Any]:
    tools = [s for s in spans if str(s.get("name", "")).startswith("tool.")]
    gens = [s for s in spans if s.get("name") == "chat.completions"]
    root = next((s for s in spans if _bench_root(s)), None)
    name = root.get("name") if root else (spans[0].get("name") if spans else "?")

    # 工具层
    tool_dur = [s.get("attributes", {}).get("duration_ms", 0) for s in tools]
    tool_dur = [d for d in tool_dur if isinstance(d, (int, float))]
    retries = sum(
        1 for s in tools if (s.get("attributes") or {}).get("codeforge.tool.retries", 0) > 0
    )
    timeouts = sum(
        1 for s in tools if (s.get("attributes") or {}).get("codeforge.tool.timeout") is True
    )
    errors = sum(1 for s in tools if s.get("status") in (2, "ERROR"))
    # 循环层
    iterations = [
        (s.get("attributes") or {}).get("codeforge.iteration", None) for s in gens
    ]
    iterations = [i for i in iterations if i is not None]
    ctx_chars = [
        (s.get("attributes") or {}).get("codeforge.iteration_context_chars", None)
        for s in gens
    ]
    ctx_chars = [c for c in ctx_chars if c is not None]
    out_toks = [
        (s.get("attributes") or {}).get("gen_ai.usage.output_tokens", None) for s in gens
    ]
    out_toks = [t for t in out_toks if t is not None]

    return {
        "name": name,
        "trace_id": str(spans[0].get("trace_id", "")),
        "tool_found": bool(tools),
        "tool_count": len(tools),
        "tool_total_ms": sum(tool_dur),
        "tool_avg_ms": round(sum(tool_dur) / len(tool_dur), 1) if tool_dur else 0,
        "tool_retries": retries,
        "tool_timeouts": timeouts,
        "tool_errors": errors,
        "llm_calls": len(gens),
        "iterations": max(iterations) if iterations else 0,
        "ctx_start_chars": ctx_chars[0] if ctx_chars else None,
        "ctx_end_chars": ctx_chars[-1] if ctx_chars else None,
        "ctx_growth_chars": (ctx_chars[-1] - ctx_chars[0]) if len(ctx_chars) > 1 else 0,
        "out_tok_first": out_toks[0] if out_toks else None,
        "out_tok_last": out_toks[-1] if out_toks else None,
    }


def print_report(groups: dict[str, list[dict[str, Any]]], trace_filter: str | None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    rows = []
    for tid, spans in groups.items():
        if not any(_bench_root(s) for s in spans):
            continue
        r = analyze_trace(spans)
        if trace_filter and r["trace_id"] != trace_filter:
            continue
        rows.append(r)
    rows.sort(key=lambda r: r.get("iterations", 0), reverse=True)

    if not rows:
        console.print("[yellow]没有找到 bench:* 根 trace。先跑一次 benchmark 再诊断。[/yellow]")
        return

    table = Table(title="CodeForge 瓶颈层级诊断")
    table.add_column("item")
    table.add_column("iters")
    table.add_column("llm")
    table.add_column("tools")
    table.add_column("tool_ms")
    table.add_column("retry")
    table.add_column("tout")
    table.add_column("err")
    table.add_column("ctx_b")
    table.add_column("ctx_e")
    table.add_column("growth")
    for r in rows:
        ctx = r.get("ctx_start_chars")
        ctxe = r.get("ctx_end_chars")
        table.add_row(
            r["name"],
            str(r["iterations"]),
            str(r["llm_calls"]),
            str(r["tool_count"]),
            str(r["tool_total_ms"]),
            str(r["tool_retries"]),
            str(r["tool_timeouts"]),
            str(r["tool_errors"]),
            str(ctx) if ctx else "-",
            str(ctxe) if ctxe else "-",
            str(r["ctx_growth_chars"]),
        )
    console.print(table)

    # 总结每条的瓶颈读数
    for r in rows:
        console.print(f"\n[bold cyan]{r['name']}[/bold cyan] (iters={r['iterations']}, "
                      f"tools={r['tool_count']}ms={r['tool_total_ms']}, "
                      f"retry={r['tool_retries']}, timeout={r['tool_timeouts']}, "
                      f"err={r['tool_errors']})")
        if r["ctx_start_chars"] is not None:
            console.print(f"  context字符: {r['ctx_start_chars']} -> {r['ctx_end_chars']} "
                          f"(增速 {r['ctx_growth_chars']})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="codeforge-diag", description="读本地 JSONL 诊断瓶颈层级")
    p.add_argument("--obs-dir", default=str(_DEFAULT_OBS), help="obs 目录(默认 ~/.codeforge/obs)")
    p.add_argument("--trace", default=None, help="只看某个 trace_id")
    args = p.parse_args(argv)

    obs = Path(args.obs_dir) if args.obs_dir else _DEFAULT_OBS
    spans = _load_spans(obs)
    if not spans:
        print(f"no spans at {obs / 'traces' / 'data.jsonl'}")
        return 1
    groups = _group_by_trace(spans)
    print_report(groups, args.trace)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
