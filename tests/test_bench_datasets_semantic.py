"""T5 语义数据集判别力验证：黑盒执行区分正确 vs 文本像但语义错。"""

from __future__ import annotations

from pathlib import Path

from benchmark.datasets import get_dataset
from benchmark.evaluators.semantic import evaluate_semantic


SMOKE = get_dataset("smoke")


def _item(name: str) -> dict:
    return next(i for i in SMOKE if i["name"] == name)


def _write_module(tmp: Path, name: str, src: str):
    (tmp / name).write_text(src, encoding="utf-8")


HAVERSINE_CORRECT = (
    "import math\n"
    "def haversine(lat1, lon1, lat2, lon2):\n"
    "    R = 6371.0\n"
    "    rlat1, rlat2 = map(math.radians, (lat1, lat2))\n"
    "    rlon1, rlon2 = map(math.radians, (lon1, lon2))\n"
    "    dlat = rlat2 - rlat1\n"
    "    dlon = rlon2 - rlon1\n"
    "    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2\n"
    "    return 2 * R * math.asin(math.sqrt(a))\n"
)


def test_city_distance_correct_impl_passes(tmp_path: Path):
    _write_module(tmp_path, "distance.py", HAVERSINE_CORRECT)
    res = evaluate_semantic(_item("city_distance"), "ok", str(tmp_path))
    assert res["value"] == 1.0, res["comment"]


def test_city_distance_wrong_euclidean_fails(tmp_path: Path):
    # 欧氏距离：能给出「数值」但不是大圆距离 → 语义必须判错（文本像但不对）
    _write_module(tmp_path, "distance.py",
                  "import math\ndef haversine(lat1, lon1, lat2, lon2):\n"
                  "    return math.sqrt((lat1-lat2)**2 + (lon1-lon2)**2)\n")
    res = evaluate_semantic(_item("city_distance"), "ok", str(tmp_path))
    # 欧氏在 B→SH 大圆 1067 上严重失配（文本像但语义错）→ 必须 <1.0（抓到）
    assert res["value"] < 1.0, res["comment"]


def test_schedule_overlap_correct_passes(tmp_path: Path):
    _write_module(tmp_path, "overlap.py",
                  "def overlap(a0,a1,b0,b1):\n    return a0 < b1 and b0 < a1\n")
    res = evaluate_semantic(_item("schedule_overlap"), "ok", str(tmp_path))
    assert res["value"] == 1.0, res["comment"]


def test_schedule_overlap_wrong_boundary_fails(tmp_path: Path):
    # 用闭区间判断会误判半开相邻不重叠 [1,5)[5,9) 与空区间 → 语义必须判错
    _write_module(tmp_path, "overlap.py",
                  "def overlap(a0,a1,b0,b1):\n    return a0 <= b1 and b0 <= a1\n")
    res = evaluate_semantic(_item("schedule_overlap"), "ok", str(tmp_path))
    assert res["value"] < 1.0, res["comment"]


def test_dataset_has_negations_and_cases():
    n_cases = sum(1 for i in SMOKE if i.get("cases"))
    assert n_cases >= 6, f"期望 ≥6 条有 cases，实得 {n_cases}"
    neg = _item("city_distance").get("negations")
    assert neg, "city_distance 应带负向断言"
