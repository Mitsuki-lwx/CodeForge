"""edges 边界数据集单测：schema 合法 + evaluate_multi 向后兼容。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from benchmark.datasets import get_dataset, list_dataset_names
from benchmark.datasets.edges import EDGES
from benchmark.evaluators import evaluate_multi
from conversation.manager import ConversationManager
from core.agent.agent import Agent
from core.agent.config import AgentConfig
from core.tool.context import ExecutionContext

VALID_OUTCOMES = {
    "denied",
    "timeout",
    "retry",
    "error",
    "empty_zero",
    "concurrency_cap",
    "serialized",
    "over",
    "safe",
}


def test_edges_registered():
    assert "edges" in list_dataset_names()
    assert get_dataset("edges") == EDGES


def test_all_edge_items_well_formed():
    names = set()
    for it in EDGES:
        assert it["name"], "name 必填"
        assert it["task"], "task 必填"
        assert it["metric"] in {"contains", "exact", "regex", "pytest_pass"}, it[
            "metric"
        ]
        assert "seed_files" in it and isinstance(it["seed_files"], dict)
        assert it["name"] not in names, f"name 重复: {it['name']}"
        names.add(it["name"])
        # edge 声明必须存在且 expected_outcome 合法
        edge = it["edge"]
        assert edge and edge["expected_outcome"] in VALID_OUTCOMES, it["name"]
        assert edge.get("span"), f"edge 需声明 span: {it['name']}"
        # guaranteed 必填且为 bool：标注该边界是构造保证触发还是依赖模型配合
        assert isinstance(edge.get("guaranteed"), bool), it["name"]


def test_edges_covers_failure_paths():
    outcomes = {it["edge"]["expected_outcome"] for it in EDGES}
    # 期望覆盖失败/异常路径，而非全正常
    assert outcomes & {"denied", "timeout", "error", "over", "serialized", "empty_zero"}


def test_edge_items_have_expected_output_for_metric():
    for it in EDGES:
        m = it["metric"]
        if m in ("contains", "exact"):
            assert it.get("expected_output"), it["name"]
        elif m == "regex":
            assert it.get("regex") or it.get("expected_output"), it["name"]


def test_evaluate_multi_backward_compat_returns_five_keys():
    # 无 steps 的普通 item：steps 维度 value=None，其余四维不改变
    item = {
        "name": "plain",
        "task": "hello",
        "metric": "contains",
        "expected_output": "hi",
        "seed_files": {},
    }
    res = evaluate_multi(item, "hi", {"usage": {}, "tool_calls": 1, "elapsed_s": 1.0})
    assert set(res.keys()) == {
        "semantic",
        "conformance",
        "quality",
        "efficiency",
        "steps",
    }
    assert res["steps"]["value"] is None


def test_evaluate_multi_steps_dim_present():
    # 带 steps 的 item：steps 维度给出加权总分
    item = {
        "name": "stepped",
        "task": "write fib",
        "metric": "contains",
        "expected_output": "55",
        "steps": [
            {"name": "fib_out", "check": "contains", "contains": ["55"], "weight": 1},
        ],
        "seed_files": {},
    }
    res = evaluate_multi(
        item, "result is 55", {"usage": {}, "tool_calls": 1, "elapsed_s": 1.0}
    )
    assert res["steps"]["value"] == 1.0


# ── deny_tools 权限拒绝机制（确定性，不依赖模型行为）────────────────


class _Cfg:
    model = "x"
    context_window = 200000


class _Client:
    config = _Cfg()


def _agent():
    tmpdir = tempfile.mkdtemp()
    return Agent(
        registry=None,
        llm_client=_Client(),
        exec_ctx=ExecutionContext(cwd=Path(tmpdir)),
        conversation=ConversationManager(),
        config=AgentConfig(max_iterations=2),
        runtime=None,
    )


def test_deny_tools_forces_deny_for_listed_tool():
    from llm.stream_events import ToolUse

    agent = _agent()
    agent.set_deny_tools(["bash"])
    dec = agent._check_tool_permission(ToolUse(id="x", name="bash", input={}))
    assert dec.effect == "deny"
    assert "deny_tools" in dec.reason


def test_deny_tools_default_does_not_change_behavior():
    from llm.stream_events import ToolUse

    agent = _agent()
    # 未 set_deny_tools：不命中 deny 分支（Default 模式 bash → ask，而非 deny）
    dec = agent._check_tool_permission(ToolUse(id="y", name="bash", input={}))
    assert dec.effect != "deny"


def test_edge_item_declares_deny_tools():
    it = next(i for i in EDGES if i["name"] == "edge_permission_denied")
    assert it["edge"]["deny_tools"] == ["bash"]
