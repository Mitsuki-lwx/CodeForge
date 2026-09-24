"""Jev 审批审查后端（`core.permissions.jev_reviewer`）单元测试。

单测**不打真实 API** —— 用 `_patch_post` 替掉 `JevReviewer._post`。
真实调用另有两份（`.workbuddy-ai/verify_jev_reviewer_live.py` 与
`verify_jev_in_agent.py`，需要 key）。
"""

from __future__ import annotations

import asyncio
import time
import urllib.error

import pytest

from core.permissions.jev_reviewer import (
    JEV_QUESTIONS,
    JevReviewer,
    build_jev_request,
    compose_reason,
    compute_confidence,
    decide_from_dimensions,
    parse_jev_response,
    risk_from_score,
)
from core.permissions.reviewer import ReviewContext

TIMEOUT_S = 0.05


def _ctx(**kw) -> ReviewContext:
    base = {
        "tool_name": "bash",
        "tool_input": {"command": "rm -rf C:/Users/x/Documents/important"},
        "category": "command",
        "permission_reason": "路径在工作区之外",
        "cwd": "D:/proj",
        "user_intent": ["帮我整理一下项目文件"],
    }
    base.update(kw)
    return ReviewContext(**base)


def _payload(
    score: float = 0.1,
    in_scope: float = 0.9,
    reversible: float = 0.9,
    risk_conf: float = 0.9,
) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "risk_level": {"type": "score", "score": score, "confidence": risk_conf},
            "in_scope": {"type": "noul", "noul": in_scope},
            "reversible": {"type": "noul", "noul": reversible},
        },
    }


def _patch_post(monkeypatch, fn) -> None:
    """替掉 `JevReviewer._post`。

    ⚠️ 必须包 `staticmethod`：直接把普通函数设成类属性会走**描述符协议**，
    于是 `self._post(payload)` 变成 `fn(self, payload)` —— 参数对不上，
    测试会以一个看似无关的 TypeError 失败。
    """
    monkeypatch.setattr(JevReviewer, "_post", staticmethod(fn))


def _reviewer() -> JevReviewer:
    return JevReviewer("fake-key", timeout=TIMEOUT_S)


# ── 1. 请求组装 ──────────────────────────────────────────────────


def test_request_shape():
    req = build_jev_request(_ctx())
    assert set(req) == {"state", "model", "questions"}
    assert req["model"] == "jev-latest"


def test_request_asks_only_atomic_dimensions():
    """★ 只问**单因素**维度，**不问**笼统的 verdict。

    实测教训：带 verdict 的版本在同一输入上 allow/deny 摇摆（confidence 仅
    0.06~0.09），而三个原子维度两次完全一致。这条把"不许再加回复合问题"
    钉死 —— 若有人加回 `verdict`，这里会失败。
    """
    assert set(JEV_QUESTIONS) == {"risk_level", "in_scope", "reversible"}
    assert "verdict" not in JEV_QUESTIONS
    assert "choice" not in {q["type"] for q in JEV_QUESTIONS.values()}


def test_request_questions_are_well_formed():
    for name, q in JEV_QUESTIONS.items():
        assert q["type"] in ("score", "noul"), name
        assert q["instructions"].strip(), f"{name} 缺 instructions"
    assert len(JEV_QUESTIONS["risk_level"]["criteria"]) == 3


def test_request_state_has_no_json_output_instruction():
    """Jev 的答案类型由**问题定义**决定 —— state 里不该带"输出 JSON"这类指令。"""
    state = build_jev_request(_ctx())["state"]
    assert "JSON" not in state
    assert "json" not in state


def test_request_state_reuses_shared_context():
    """state 必须含共用上下文（工具/参数/cwd/用户意图）——
    与 LLM 后端输入一致，判定结果才可对照。"""
    state = build_jev_request(_ctx())["state"]
    assert "bash" in state
    assert "D:/proj" in state
    assert "帮我整理一下项目文件" in state


# ── 2. 组合规则（本次的核心逻辑）────────────────────────────────


@pytest.mark.parametrize(
    ("score", "in_scope", "reversible", "expected"),
    [
        # 三项都明确通过 → 放行
        (0.1, 0.9, 0.9, True),
        (0.0, 1.0, 1.0, True),
        (1.0, 0.6, 0.6, True),  # 中等风险仍可放行（未到 high）
        # 任一维度不通过 → 拒
        (0.1, 0.4, 0.9, False),  # 超出授权
        (0.1, 0.9, 0.4, False),  # 不可逆
        (1.5, 0.9, 0.9, False),  # 高风险
        (2.0, 1.0, 1.0, False),  # 高风险
        (1.34, 0.9, 0.9, False),  # high 档下界
        (1.33, 0.9, 0.9, True),  # medium 档上界
    ],
)
def test_decide_combines_dimensions(score, in_scope, reversible, expected):
    assert decide_from_dimensions(score, in_scope, reversible) is expected


@pytest.mark.parametrize(
    ("score", "in_scope", "reversible"),
    [
        (None, 0.9, 0.9),  # 风险说不清
        (0.1, None, 0.9),  # 授权说不清
        (0.1, 0.9, None),  # 可逆性说不清
    ],
)
def test_decide_is_fail_closed_on_missing_dimension(score, in_scope, reversible):
    """**有一项说不清就不放行** —— 无人值守下这不是"多数表决"。"""
    assert decide_from_dimensions(score, in_scope, reversible) is False


# ── 3. 响应解析 ──────────────────────────────────────────────────


def test_parse_allow():
    out = parse_jev_response(_payload())
    assert out.allowed is True
    assert out.risk == "low"
    assert "放行" in out.reason


def test_parse_deny_out_of_scope():
    out = parse_jev_response(_payload(in_scope=0.06))
    assert out.allowed is False
    assert "超出授权" in out.reason


def test_parse_deny_high_risk():
    out = parse_jev_response(_payload(score=1.9))
    assert out.allowed is False
    assert out.risk == "high"


def test_parse_deny_irreversible():
    out = parse_jev_response(_payload(reversible=0.1))
    assert out.allowed is False
    assert "不可逆" in out.reason


def test_parse_is_deterministic_on_same_payload():
    """同一响应反复解析结果必须一致（纯函数）。

    真实模型可能摇摆，但**解析层不许**再引入不确定性 —— 这是"结论只由三个
    维度决定"的直接体现。
    """
    data = _payload()
    outs = {parse_jev_response(data).allowed for _ in range(10)}
    assert outs == {True}


@pytest.mark.parametrize(
    "bad",
    [
        "not a dict",
        [],
        None,
        42,
        {},  # 缺 answers
        {"answers": "nope"},  # answers 不是对象
        {"answers": {}},  # 三个维度全缺
        {"answers": {"verdict": {"choice": "allow"}}},  # 旧的复合格式 → 全缺
    ],
)
def test_parse_rejects_invalid(bad):
    with pytest.raises((TypeError, ValueError)):
        parse_jev_response(bad)


def test_parse_tolerates_partial_dimensions():
    """少一个维度不是"响应不合法"，而是**该维度说不清 → 拒**（fail-closed）。"""
    data = {"answers": {"in_scope": {"noul": 0.9}, "reversible": {"noul": 0.9}}}
    out = parse_jev_response(data)
    assert out.allowed is False  # risk 缺失 → 拒
    assert "风险" not in out.reason  # 缺失的维度不写进 reason


# ── 4. 置信度与 risk 边界 ────────────────────────────────────────


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0.0, "low"), (0.66, "low"), (0.67, "medium"), (1.33, "medium"), (1.34, "high")],
)
def test_risk_band_boundaries(score, expected):
    assert risk_from_score(score) == expected


def test_confidence_takes_the_weakest_dimension():
    """一个维度拿不准，整体就不该被高看。"""
    assert compute_confidence(0.9, 1.0, 1.0) == pytest.approx(0.9)
    assert compute_confidence(0.9, 0.5, 1.0) == pytest.approx(0.0)  # 0.5 → 完全不确定
    assert compute_confidence(0.9, 0.9, 0.1) == pytest.approx(0.8)  # |0.1-0.5|*2
    assert compute_confidence(None, None, None) is None


def test_confidence_present_on_normal_payload():
    out = parse_jev_response(_payload(risk_conf=0.8, in_scope=0.95, reversible=1.0))
    assert out.confidence == pytest.approx(0.8)


# ── 5. reason ────────────────────────────────────────────────────


def test_compose_reason_is_attributable():
    """reason 要**可复查**：每个词都对应一个数字，不许出现"存在风险"这类空话。"""
    reason = compose_reason(False, 1.9, 0.06, 0.1)
    assert "拒绝" in reason
    assert "1.90" in reason
    assert "0.06" in reason
    assert "超出授权" in reason
    assert "不可逆" in reason


def test_compose_reason_omits_missing_dimensions():
    reason = compose_reason(False, None, None, None)
    assert "拒绝" in reason
    assert "风险" not in reason
    assert "授权" not in reason


# ── 6. review()：fail-closed，三类文案可区分 ─────────────────────


def test_timeout_fails_closed(monkeypatch):
    def slow(_payload):
        time.sleep(0.3)
        return {}

    _patch_post(monkeypatch, slow)
    out = asyncio.run(_reviewer().review(_ctx()))
    assert out.allowed is False
    assert "超时" in out.reason


def test_http_error_fails_closed(monkeypatch):
    def boom(_payload):
        raise urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)

    _patch_post(monkeypatch, boom)
    out = asyncio.run(_reviewer().review(_ctx()))
    assert out.allowed is False
    assert "429" in out.reason


def test_network_error_fails_closed(monkeypatch):
    def boom(_payload):
        raise OSError("connection reset")

    _patch_post(monkeypatch, boom)
    out = asyncio.run(_reviewer().review(_ctx()))
    assert out.allowed is False
    assert "失败" in out.reason


def test_invalid_response_fails_closed(monkeypatch):
    _patch_post(monkeypatch, lambda _p: {"answers": {}})
    out = asyncio.run(_reviewer().review(_ctx()))
    assert out.allowed is False
    assert "不合法" in out.reason


def test_three_failure_reasons_are_distinguishable(monkeypatch):
    """三种失败的原因必须**互不相同** —— 事后要能判断"判错了"还是"没跑起来"。"""

    def slow(_p):
        time.sleep(0.3)
        return {}

    def http_err(_p):
        raise urllib.error.HTTPError("http://x", 500, "Server Error", {}, None)

    reasons = []
    _patch_post(monkeypatch, slow)
    reasons.append(asyncio.run(_reviewer().review(_ctx())).reason)
    _patch_post(monkeypatch, http_err)
    reasons.append(asyncio.run(_reviewer().review(_ctx())).reason)
    _patch_post(monkeypatch, lambda _p: {"answers": {}})
    reasons.append(asyncio.run(_reviewer().review(_ctx())).reason)

    assert len(set(reasons)) == 3, reasons


def test_review_returns_parsed_outcome(monkeypatch):
    """`review()` 端到端：解析结果必须**原样传出去**。"""
    _patch_post(monkeypatch, lambda _p: _payload(in_scope=0.06))
    out = asyncio.run(_reviewer().review(_ctx()))
    assert out.allowed is False
    assert "超出授权" in out.reason


# ── 7. 接口与构造 ────────────────────────────────────────────────


def test_interface_matches_llm_reviewer():
    """两个后端必须同签名 —— 否则 `agent.py` 的调用点就要分叉。"""
    import inspect

    from core.permissions.reviewer import ApprovalReviewer

    assert list(inspect.signature(JevReviewer.review).parameters) == list(
        inspect.signature(ApprovalReviewer.review).parameters
    )
    assert isinstance(JevReviewer.timeout, property)
    assert isinstance(ApprovalReviewer.timeout, property)
    assert JevReviewer("k", timeout=7).timeout == 7


def test_missing_api_key_raises():
    with pytest.raises(ValueError):
        JevReviewer("")


def test_does_not_import_llm_layer():
    """它**不是** chat 模型，不该沾 `Adapter` / `LLMClient`。"""
    import pathlib

    src = pathlib.Path("core/permissions/jev_reviewer.py").read_text(encoding="utf-8")
    assert "from llm" not in src
    assert "import llm" not in src


# ── 8. 配置解析（loader）与装配（bootstrap）──────────────────────

_CFG_TEMPLATE = """\
providers:
  - name: t
    protocol: openai
    model: m
    api_key: k
{extra}
"""


def _write_cfg(tmp_path, extra: str) -> str:
    path = tmp_path / "c.yaml"
    path.write_text(_CFG_TEMPLATE.format(extra=extra), encoding="utf-8")
    return str(path)


def _features(path: str):
    from config.loader import load_config_full

    return load_config_full(path)[1].approval_review


def test_config_absent_means_default_backend(tmp_path):
    """整段不配 = 走**默认后端**（`jev`），不是报错、也不是 llm。

    配置对象本身是 `None`（没有这一段），默认值由解析层给出 ——
    所以这里同时钉住两件事。
    """
    path = _write_cfg(tmp_path, "")
    cfg = _features(path)
    assert cfg is None  # 没有这一段

    from core.agent.bootstrap import _resolve_review_backend

    backend, jev_cfg = _resolve_review_backend(path)
    assert backend == "jev"
    assert jev_cfg is None  # 没配 jev 子段 ⇒ 没有 key


def test_dataclass_default_backend_is_jev():
    """数据类默认值必须是 `jev` —— 文档写的默认值要真的是默认值。

    没有这条时，"默认值是 jev" 只由 loader 里的字符串兜底保证，
    数据类自身改回 llm 也测不出来。
    """
    from config.model import ApprovalReviewConfig

    assert ApprovalReviewConfig().backend == "jev"


def test_loader_empty_dict_defaults_to_jev():
    """`approval_review` 是个空 dict 时 → 也是 `jev`（不是 llm）。"""
    from config.loader import _parse_approval_review_config

    assert _parse_approval_review_config({}).backend == "jev"


def test_config_jev_with_key(tmp_path):
    cfg = _features(
        _write_cfg(
            tmp_path,
            "features:\n  approval_review:\n    backend: jev\n"
            "    jev:\n      api_key: fake-key\n      timeout_s: 9\n",
        )
    )
    assert cfg.backend == "jev"
    assert cfg.jev.api_key == "fake-key"
    assert cfg.jev.timeout_s == 9
    assert cfg.jev.model == "jev-latest"


def test_config_jev_without_key_does_not_fall_back(tmp_path, capsys):
    """backend=jev 但没 key → **不回落 llm**（loader 层不做任何替换）。

    "没有审查者"由装配层决定并大声说出，不在这层偷偷换后端 ——
    静默换后端正是本次要消除的行为（`docs/spec_jev_default.md`）。
    """
    cfg = _features(
        _write_cfg(tmp_path, "features:\n  approval_review:\n    backend: jev\n")
    )
    assert cfg.backend == "jev"  # 保持原样，没有被改成 llm
    err = capsys.readouterr().err
    assert "回落" not in err  # loader 不再报"已回落"


def test_config_invalid_backend_falls_back_to_default(tmp_path, capsys):
    """非法取值 → 告警 + 退回**默认（jev）**，不是 llm。"""
    cfg = _features(
        _write_cfg(tmp_path, "features:\n  approval_review:\n    backend: wat\n")
    )
    assert cfg.backend == "jev"
    err = capsys.readouterr().err
    assert "wat" in err
    assert "jev" in err  # 告警里要说清退回到哪里


def test_config_invalid_timeout_falls_back(tmp_path, capsys):
    cfg = _features(
        _write_cfg(
            tmp_path,
            "features:\n  approval_review:\n    backend: jev\n    jev:\n"
            "      api_key: k\n      timeout_s: not-a-number\n",
        )
    )
    assert cfg.jev.timeout_s == 15.0
    assert "timeout_s" in capsys.readouterr().err


def test_config_negative_timeout_falls_back(tmp_path, capsys):
    cfg = _features(
        _write_cfg(
            tmp_path,
            "features:\n  approval_review:\n    backend: jev\n    jev:\n"
            "      api_key: k\n      timeout_s: -3\n",
        )
    )
    assert cfg.jev.timeout_s == 15.0
    assert "timeout_s" in capsys.readouterr().err


def test_bootstrap_builds_jev_reviewer(tmp_path):
    """装配：backend=jev + key → 真的造出 `JevReviewer`（不需要 client）。"""
    from core.agent.bootstrap import _build_approval_reviewer

    path = _write_cfg(
        tmp_path,
        "features:\n  approval_review:\n    backend: jev\n    jev:\n"
        "      api_key: fake-key\n      timeout_s: 11\n",
    )
    notices: list[str] = []
    reviewer = _build_approval_reviewer(None, notices, path)
    assert isinstance(reviewer, JevReviewer)
    assert reviewer.timeout == 11
    assert any("Jev" in n for n in notices)


def test_bootstrap_jev_without_key_never_falls_back_to_llm(tmp_path):
    """key 为空 → **不启用审查**，且**明确说"没有回落到 LLM"**。

    这是本次改动的核心：宁可没有审查者（`REVIEW` 档会降级成 `deny_all`，变严），
    也不静默改用另一个后端。**给一个能用的 client 也一样** —— 不留后门。
    """
    from config.model import ProviderConfig
    from core.agent.bootstrap import _build_approval_reviewer
    from llm.client import LLMClient

    path = _write_cfg(
        tmp_path,
        "features:\n  approval_review:\n    backend: jev\n    jev:\n      api_key: ''\n",
    )
    client = LLMClient.create(
        ProviderConfig(name="t", protocol="openai", model="m", api_key="sk-x")
    )
    notices: list[str] = []
    reviewer = _build_approval_reviewer(client, notices, path)
    assert reviewer is None
    joined = " ".join(notices)
    assert "没有回落到" in joined, notices
    assert "api_key" in joined


def test_bootstrap_broken_jev_config_never_falls_back_to_llm(tmp_path, monkeypatch):
    """Jev 构造失败 → 同样**不回落** llm，返回 None + notice。

    用 monkeypatch 让构造函数真的抛（`JevReviewer` 不校验 url，光配个假 url
    不会失败 —— 这点也是实测出来的）。
    """
    import core.permissions.jev_reviewer as jev_mod
    from config.model import ProviderConfig
    from core.agent.bootstrap import _build_approval_reviewer
    from llm.client import LLMClient

    def _boom(*_a, **_k):
        raise RuntimeError("构造故意失败")

    monkeypatch.setattr(jev_mod, "JevReviewer", _boom)

    path = _write_cfg(
        tmp_path,
        "features:\n  approval_review:\n    backend: jev\n    jev:\n"
        "      api_key: fake\n",
    )
    client = LLMClient.create(
        ProviderConfig(name="t", protocol="openai", model="m", api_key="sk-x")
    )
    notices: list[str] = []
    reviewer = _build_approval_reviewer(client, notices, path)
    assert reviewer is None
    assert any("没有回落到" in n for n in notices), notices


def test_bootstrap_default_backend_is_jev_not_llm(tmp_path):
    """不配 `approval_review` 段 → 默认走 **jev**；没有 key 就没有审查者。

    关键断言：**即使给了一个完好的 LLM client，也不会造出 LLM 审查者**。
    """
    from config.model import ProviderConfig
    from core.agent.bootstrap import _build_approval_reviewer
    from llm.client import LLMClient

    path = _write_cfg(tmp_path, "")
    client = LLMClient.create(
        ProviderConfig(name="t", protocol="openai", model="m", api_key="sk-x")
    )
    notices: list[str] = []
    reviewer = _build_approval_reviewer(client, notices, path)
    assert reviewer is None, "默认是 jev，没有 key 就不该造出 LLM 审查者"
    assert any("Jev" in n for n in notices)


def test_bootstrap_no_config_path_resolves_to_jev():
    """没给配置来源 → 也按默认（jev）解析，**不回到 llm**。"""
    from core.agent.bootstrap import _resolve_review_backend

    assert _resolve_review_backend(None) == ("jev", None)
    assert _resolve_review_backend("") == ("jev", None)


def test_bootstrap_explicit_llm_backend_still_works(tmp_path):
    """显式 `backend: llm` 仍能造出 `ApprovalReviewer`（零回归）。

    LLM 后端是**显式可选项**，不是默认、也不是回落 —— 这条路必须留着。
    """
    from config.model import ProviderConfig
    from core.agent.bootstrap import _build_approval_reviewer
    from llm.client import LLMClient

    path = _write_cfg(tmp_path, "features:\n  approval_review:\n    backend: llm\n")
    client = LLMClient.create(
        ProviderConfig(
            name="t",
            protocol="openai",
            model="main-model",
            api_key="sk-x",
            model_aliases={"haiku": "cheap-model"},
        )
    )
    notices: list[str] = []
    reviewer = _build_approval_reviewer(client, notices, path)
    assert reviewer is not None
    assert not isinstance(reviewer, JevReviewer)
    assert reviewer._client.config.model == "cheap-model"  # 仍走 haiku 便宜档
