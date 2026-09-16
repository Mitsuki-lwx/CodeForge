"""黑盒执行 + 语义等价判定。

`run_exe` 在指定 cwd 里执行一个命令（默认 `sys.executable`，可传 cmd 数组），
返回 `(returncode, stdout, stderr)`——复用 `benchmark/evaluators.py` 跑子进程的范式
（venv python + timeout + capture）。

`semantic_equiv(actual, want, kind)` 按语义归约比较两段文本/结构化数据是否等价：
  number≈   解析浮点，容差 1e-3（避免 3.0000001 vs 3.0 误判）
  set       解析集合/数组，无序比较
  lines     按行拆，忽略首尾空白/空行后逐行等价
  json      深度结构化比较（键无序、数值容差）

都返回 (0|1, comment)。这是对「只靠字符串子串命中」的替代：判断『行为正确』
而非『文本像不像』。
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from typing import Any

_RTOL = 1e-3  # 数值相对容差
_ATOL = 1e-6  # 数值绝对容差（覆盖接近 0 的情况）


def run_exe(cwd: str, cmd: list[str] | None = None, input_text: str | None = None) -> tuple[int, str, str]:
    """在 cwd 里跑 cmd（默认 python -c 读 stdin），返回 (rc, stdout, stderr)。

    Args:
        cwd: 工作目录（通常是 agent 写产物的临时目录）。
        cmd: 要执行的命令；None 时用 `python -c "<input_text>"`——但为安全，
            None + input_text 会被当作脚本内容执行。
        input_text: 按 `cases[i].input` 喂给待测程序的标准输入。
    Raises:
        subprocess.TimeoutExpired: 超时（调用方捕获）。
        FileNotFoundError: 找不到解释器/可执行文件。
    """
    if cmd is None:
        # 默认把输入当 Python 脚本源执行（用于评测 Python 任务产物的典型路径）
        assert input_text is not None, "cmd 与 input_text 必给其一"
        argv = [sys.executable, "-c", input_text]
    else:
        argv = cmd
    proc = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        input=input_text,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _to_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.strip().splitlines() if ln.strip()]


def semantic_equiv(actual: str, want: str, kind: str) -> tuple[int, str]:
    """按 kind 归约比较 actual 与 want。返回 (1|0, comment)。"""
    if kind in ("number≈", "number"):
        a, w = _to_float(actual.strip()), _to_float(want.strip())
        if a is None or w is None:
            return 0, f"无法解析数值 actual={actual!r}"
        # 相对容差（allclose 语义）：比值为 0 时退到绝对容差
        ok = abs(a - w) <= _ATOL + _RTOL * abs(w)
        return (1 if ok else 0), (f"数值近似 {a:.6g}≈{w:.6g}" if ok
                                   else f"数值不近 {a:.6g} vs {w:.6g}")

    if kind in ("set", "unordered"):
        try:
            a = sorted(actual.strip())
            w = sorted(want.strip())
        except Exception:  # noqa: BLE001
            return 0, "集合解析失败"
        # 先比唯一字符集合的直方图（无序但计重复）
        a_hist = {ch: a.count(ch) for ch in set(a)}
        w_hist = {ch: w.count(ch) for ch in set(w)}
        return (1 if a_hist == w_hist else 0), (
            "无序字符直方图一致" if a_hist == w_hist else f"字符直方图不一致 {a_hist} vs {w_hist}"
        )

    if kind in ("lines", "text"):
        a = _parse_lines(actual)
        w = _parse_lines(want)
        return (1 if a == w else 0), (
            "行序列等价（忽略空白/空行）" if a == w
            else f"行不等 actual={a!r} want={w!r}"
        )

    if kind in ("json", "json≈"):
        try:
            a = json.loads(actual)
            w = json.loads(want)
        except json.JSONDecodeError as e:
            return 0, f"JSON 解析失败: {e.msg}"
        ok = _json_deep_eq(a, w)
        return (1 if ok else 0), ("JSON 结构等价" if ok else "JSON 结构不等")

    return 0, f"未知归约 kind={kind!r}"


def _json_deep_eq(a: Any, b: Any) -> bool:
    """结构化等价：dict 键无序、float 容差、list 需有序且逐项相等。"""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_json_deep_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_json_deep_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, str)) and isinstance(b, (int, str)):
        return a == b
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= _ATOL + _RTOL * abs(b)
    return a == b


def evaluate_semantic(item: dict, output: str, cwd: str | None = None) -> dict:
    """对一条 item 做语义等价评测。

    三种输入决定怎么评：
      - item['reference-exec'] 为一个**可执行命令**（list）：在 cwd 跑 reference 拿
        want，跑 item 的产物（`item['cmd']` 或 `item['script']`）拿 actual，比 output。
      - item['cases'] 列表：每个 {input, want, expect_error?, kind?}，把 input 喂给
        `item['cmd']` 或 `item['script']`，对 stdout 做 semantic_equiv；负向者要
        returncode!=0。
      - 二者都没有：退化为把 `output`（agent 文本摘要）与 `item['reference']`
        按 `item['kind']`（默认 text）做 semantic_equiv（轻量、不执行）。
    cwd 缺失时只做第三种（安全的文本归约），不执行。
    """
    cases = item.get("cases")
    cmd = item.get("cmd") or (item.get("script") and [sys.executable, "-c", item["script"]])
    exec_ok = cwd is not None and bool(cmd or item.get("reference-exec"))

    if not exec_ok:
        return _text_fallback(item, output)

    if cases:
        passes = []
        for c in cases:
            inp = c.get("input")
            want = c.get("want", "")
            kind = c.get("kind", "lines")
            expect_err = bool(c.get("expect_error"))
            try:
                rc, out, _err = run_exe(cwd, cmd=cmd, input_text=inp if inp is not None else "")
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                passes.append(0)
                continue
            if expect_err:
                ok = rc != 0
                passes.append(1 if ok else 0)
            else:
                ok, _k = semantic_equiv(out, str(want), kind)
                passes.append(ok)
        if not passes:
            return _mk(None, "cases 为空")
        return _mk(
            1.0 if all(p == 1 for p in passes) else
            (sum(passes) / len(passes)),
            f"{sum(passes)}/{len(passes)} 用例语义通过",
        )

    # 无 cases：跑 reference 求 want，再跑产物求 actual，逐 case 比
    if item.get("reference-exec"):
        try:
            want_out = run_exe(cwd, cmd=item["reference-exec"])[1]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return _mk(None, "reference 执行失败")
        try:
            act_out = run_exe(cwd, cmd=cmd or [sys.executable, "-c", ""])[1]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return _mk(0, f"产物执行失败 {type(e).__name__}")
        ok, note = semantic_equiv(act_out, want_out, item.get("kind", "lines"))
        return _mk(1.0 if ok else 0.0, note)

    return _mk(None, "无 cases 亦无 reference-exec")


def _text_fallback(item: dict, output: str) -> dict:
    want = item.get("reference")
    if not want:
        return _mk(None, "无 reference，语义缺测")
    ok, note = semantic_equiv(output, str(want), item.get("kind", "text"))
    return _mk(1.0 if ok else 0.0, note)


def _mk(value: float | None, comment: str) -> dict:
    return {"name": "semantic", "value": value, "comment": comment}
