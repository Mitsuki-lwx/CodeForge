"""可观测性分析脚本。

读两处数据,聚合出「延迟/瓶颈、错误/失败率、Agent 行为」三类信号:
  - obs traces  (~/.codeforge/obs/traces/data.jsonl) —— OTel span(含 gen_ai.* 与工具 span 耗时)
  - audit      (~/.codeforge/audit/<session>.jsonl) —— agent 执行 trace(工具/权限/错误/压缩)

用法:
  python scripts/analyze_obs.py                 # 分析默认位置
  python scripts/analyze_obs.py --obs PATH --audit PATH
  python scripts/analyze_obs.py --session main   # 只看某会话 audit
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HOME_OBS = Path.home() / ".codeforge" / "obs" / "traces" / "data.jsonl"
HOME_AUDIT_DIR = Path.home() / ".codeforge" / "audit"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def _ms(start: int | None, end: int | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start) / 1_000_000  # ns → ms


def analyze_traces(obs_path: Path) -> dict[str, Any]:
    spans = _read_jsonl(obs_path)
    by_name = defaultdict(list)
    durations: dict[str, list[float]] = defaultdict(list)
    genai: list[dict] = []
    errors = 0
    for s in spans:
        name = s.get("name", "?")
        by_name[name].append(s)
        d = _ms(s.get("start_time_unix_nano"), s.get("end_time_unix_nano"))
        if d is not None:
            durations[name].append(d)
        attrs = s.get("attributes") or {}
        if name == "chat.completions":
            genai.append({
                "model": attrs.get("gen_ai.request.model"),
                "in": attrs.get("gen_ai.usage.input_tokens"),
                "out": attrs.get("gen_ai.usage.output_tokens"),
                "dur_ms": d,
            })
        if s.get("attributes", {}).get("success") is False or s.get("status") == 2:
            errors += 1

    return {
        "span_counts": {k: len(v) for k, v in by_name.items()},
        "avg_duration_ms": {k: round(sum(v) / len(v), 1) for k, v in durations.items()},
        "max_duration_ms": {k: round(max(v), 1) for k, v in durations.items()},
        "slowest": sorted(
            ((name, round(max(v), 1)) for name, v in durations.items()),
            key=lambda x: x[1], reverse=True,
        ),
        "gen_ai_calls": len(genai),
        "gen_ai_tokens": {"in": sum(g["in"] or 0 for g in genai), "out": sum(g["out"] or 0 for g in genai)},
        "span_errors": errors,
    }


def analyze_audit(audit_dir: Path, only_session: str | None) -> dict[str, Any]:
    files = sorted(audit_dir.glob("*.jsonl")) if audit_dir.exists() else []
    # 跳过 pytest 测试会话生成的 audit(test.jsonl)——非真实 CodeForge 使用,会污染分析
    files = [f for f in files if f.stem != "test"]
    tools: dict[str, list[int]] = defaultdict(list)
    tool_success: Counter = Counter()
    permission_denied = 0
    permission_counts: Counter = Counter()
    errors: list[dict] = []
    compactions = 0
    hooks_blocked = 0
    agents_end = 0
    for f in files:
        if only_session and f.stem != only_session:
            continue
        for ev in _read_jsonl(f):
            e = ev.get("event")
            if e == "tool_end":
                tools[ev.get("tool_name", "?")].append(ev.get("duration_ms") or 0)
                tool_success[("ok" if ev.get("success") else "fail", ev.get("tool_name", "?"))] += 1
            elif e == "permission":
                permission_counts[ev.get("decision")] += 1
                if ev.get("decision") == "deny":
                    permission_denied += 1
            elif e == "agent_error":
                errors.append({"code": ev.get("code"), "ts": ev.get("ts")})
            elif e == "compact":
                compactions += 1
            elif e == "hook":
                if ev.get("blocked"):
                    hooks_blocked += 1
            elif e == "agent_end":
                agents_end += 1

    # 慢工具 Top
    slow_tools = sorted(
        ((name, max(v), sum(v) / len(v)) for name, v in tools.items() if v),
        key=lambda x: x[2], reverse=True,
    )
    return {
        "session_files": len(files),
        "tool_calls": sum(len(v) for v in tools.values()),
        "tool_duration_sum_ms": sum(sum(v) for v in tools.values()),
        "slowest_tools_avg_ms": [{"tool": t, "max_ms": m, "avg_ms": round(a, 1)} for t, m, a in slow_tools[:8]],
        "tool_failures": tool_success,
        "permission": {"deny": permission_denied, "all": dict(permission_counts)},
        "errors": errors,
        "error_count": len(errors),
        "err_codes": Counter(e["code"] for e in errors),
        "compactions": compactions,
        "hooks_blocked": hooks_blocked,
        "agent_runs": agents_end,
    }


def print_report(t: dict, a: dict) -> None:
    print("=" * 60)
    print("可观测性分析报告")
    print("=" * 60)

    print("\n── 延迟/瓶颈 ──")
    print("  OTel span 平均耗时(ms):")
    for name, avg in t["avg_duration_ms"].items():
        print(f"    {name:<20} avg={avg}  max={t['max_duration_ms'].get(name)}")
    print("  Audit 工具平均耗时 Top:")
    for x in a["slowest_tools_avg_ms"][:6]:
        print(f"    {x['tool']:<20} avg={x['avg_ms']}ms max={x['max_ms']}ms")
    if t["slowest"]:
        slowest = t["slowest"][0]
        print(f"  全局最慢: {slowest[0]} = {slowest[1]}ms")

    print("\n── 错误/失败率 ──")
    tool_fails = sum(f for (flag, _), f in a["tool_failures"].items() if flag == "fail")
    tool_ok = sum(f for (flag, _), f in a["tool_failures"].items() if flag == "ok")
    total = tool_fails + tool_ok
    rate = (tool_fails / total * 100) if total else 0
    print(f"  工具失败率: {tool_fails}/{total} = {rate:.1f}%")
    print(f"  span_errors(OTel): {t['span_errors']}")
    print(f"  权限拒绝: {a['permission']['deny']}")
    for code, n in a["err_codes"].items():
        print(f"  error[{code}]: {n}")

    print("\n── Agent 行为 ──")
    print(f"  agent 运行次数: {a['agent_runs']}")
    print(f"  工具总调用: {a['tool_calls']}  总耗时={a['tool_duration_sum_ms']}ms")
    print(f"  上下文压缩: {a['compactions']} 次")
    print(f"  hook 拦截: {a['hooks_blocked']} 次")
    print(f"  LLM 调用: {t['gen_ai_calls']}  总token in={t['gen_ai_tokens']['in']} out={t['gen_ai_tokens']['out']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default=str(HOME_OBS))
    ap.add_argument("--audit", default=str(HOME_AUDIT_DIR))
    ap.add_argument("--session", default=None)
    args = ap.parse_args()

    t = analyze_traces(Path(args.obs))
    a = analyze_audit(Path(args.audit), args.session)
    print_report(t, a)


if __name__ == "__main__":
    main()
