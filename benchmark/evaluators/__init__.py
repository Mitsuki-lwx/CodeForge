"""多维评测器子包（正确性/规约/质量/效率 + 旧 metric）。

本包收纳了旧的单一 metric 判定（`legacy.score_item`）并新增多维入口 `evaluate_multi`，
两者**并存且向后兼容**：
  - `benchmark.evaluators.score_item` / `.VALID_METRICS` —— 旧接口，语义不变
    （迁移自原 `benchmark/evaluators.py`，该文件因同名目录冲突已并入本包）。
  - `benchmark.evaluators.evaluate_multi` —— 新多维评测统一入口。

- legacy.py         旧单一 metric（contains/exact/regex/pytest_pass）
- semantic.py       黑盒执行语义等价（number≈/set/lines/json）
- conformance.py     输出规约（纯文本/可管道/exit/负向断言）
- quality.py        产物可维护性（AST 静态符号）
- efficiency.py     过程效率（token/工具数/耗时 + 超限降级）
"""

from __future__ import annotations

from benchmark.evaluators.legacy import VALID_METRICS as VALID_METRICS
from benchmark.evaluators.legacy import score_item as score_item


def evaluate_multi(
    item: dict, output: str, run_metrics: dict, cwd: str | None = None
) -> dict:
    """统一入口：返回各维度 score dict。

    维度 key：`semantic`/`conformance`/`quality`/`efficiency`（既有），另加第 5 键
    `steps`（步骤×权重分，`benchmark.evaluators.steps`；item 无 `steps` 时 value=None，
    保持向后兼容）。值为 {"name","value","comment"}（value 0–1 或 None=缺测）。
    """
    from benchmark.evaluators.conformance import evaluate_conformance
    from benchmark.evaluators.efficiency import evaluate_efficiency
    from benchmark.evaluators.quality import evaluate_quality
    from benchmark.evaluators.semantic import evaluate_semantic
    from benchmark.evaluators.steps import evaluate_steps

    results: dict[str, dict] = {}
    results["semantic"] = evaluate_semantic(item, output, cwd)
    results["conformance"] = evaluate_conformance(item, output)
    results["quality"] = evaluate_quality(item, cwd)
    results["efficiency"] = evaluate_efficiency(item, run_metrics)
    results["steps"] = evaluate_steps(item, output, run_metrics, cwd)
    return results
