"""CodeForge 基准数据集注册表。"""

from __future__ import annotations

from benchmark.datasets.edges import EDGES
from benchmark.datasets.smoke import SMOKE

DATASETS: dict[str, list[dict]] = {
    "smoke": SMOKE,
    "edges": EDGES,
}


def get_dataset(name: str) -> list[dict]:
    if name not in DATASETS:
        raise KeyError(f"未知数据集 {name!r}。可用：{', '.join(sorted(DATASETS))}")
    return DATASETS[name]


def list_dataset_names() -> list[str]:
    return sorted(DATASETS)
