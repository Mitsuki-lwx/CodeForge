"""SMOKE 数据集结构校验（无网络）。"""

import pytest

from benchmark.datasets import DATASETS, get_dataset
from benchmark.evaluators import VALID_METRICS


@pytest.mark.parametrize("name,items", DATASETS.items())
def test_dataset_items_well_formed(name, items):
    assert isinstance(items, list) and len(items) > 0, f"{name} 为空"
    names = [it.get("name") for it in items]
    assert len(names) == len(set(names)), f"{name} 有重复 item name"


@pytest.mark.parametrize("name,items", DATASETS.items())
def test_each_item_has_required_fields(name, items):
    for it in items:
        assert it.get("name"), f"item 缺 name"
        assert it.get("task"), f"{it.get('name')} 缺 task"
        assert it.get("metric") in VALID_METRICS, (
            f"{it.get('name')} 的 metric 非法: {it.get('metric')}"
        )
        # contains/exact/regex 必须给 expected_output 或 regex
        m = it.get("metric")
        if m in ("contains", "exact"):
            assert it.get("expected_output"), f"{it.get('name')} 缺 expected_output"
        if m == "regex":
            assert it.get("regex") or it.get("expected_output"), (
                f"{it.get('name')} 缺 regex 或 expected_output"
            )
        assert isinstance(it.get("seed_files") or {}, dict), (
            f"{it.get('name')} seed_files 必须是 dict"
        )


def test_get_dataset_lookup():
    assert get_dataset("smoke") is DATASETS["smoke"]
    with pytest.raises(KeyError):
        get_dataset("does-not-exist")
