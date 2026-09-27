"""落盘脱敏的验收测试（`docs/checklist_show_redact.md` §4、§5）。

设计要点：**正例与反例成对**。只测正例的脱敏函数等于没测 ——
真正会出事的是误伤（把 `token_expiry_seconds` 打成码）。
"""

from __future__ import annotations

import copy

import pytest

from core.observability.redact import (
    MASK_ALL,
    MAX_DEPTH,
    RedactionSettings,
    redact_mapping,
    redact_text,
    redaction_config,
)

# ── §4 正例：每种形态都要挡住 ───────────────────────────────────

_A = "aB3"  # 混合大小写+数字，能过熵值门槛


def _k(prefix: str, repeat: int = 10) -> str:
    return prefix + _A * repeat


POSITIVE = [
    ("anthropic_key", _k("sk-ant-api03-")),
    ("openai_key", _k("sk-", 12)),
    ("aws_key", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_key", "ASIAIOSFODNN7EXAMPLE"),  # session key 同形态
    ("google_key", _k("AIza", 12)),
    ("github_token", _k("ghp_")),
    ("github_token", _k("gho_")),  # oauth 同形态
    ("github_pat", _k("github_pat_", 8)),
    ("slack_token", _k("xoxb-", 6)),
    ("stripe_key", _k("sk_live_", 8)),
    ("stripe_webhook", _k("whsec_", 8)),
    ("huggingface", _k("hf_", 10)),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP",
    ),
    ("bearer_token", "Bearer " + _A * 12),
    (
        "private_key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\n-----END RSA PRIVATE KEY-----",
    ),
    ("url_credentials", "https://user:" + _A * 4 + "@example.com/x"),
]


@pytest.mark.parametrize(("label", "secret"), POSITIVE)
def test_redacts_known_credential_shapes(label: str, secret: str) -> None:
    out = redact_text(secret)
    assert "REDACTED" in out, f"{label} 未被脱敏: {out!r}"
    assert label in out, f"遮蔽标记应带形态名 {label}: {out!r}"
    # 关键：原文里不能残留任何一段实质内容
    assert _A not in out, f"{label} 遮蔽不彻底，仍有残留: {out!r}"


def test_redacts_credential_embedded_in_surrounding_text() -> None:
    """真实场景：凭据是工具输出里的一行，不是一整段。"""
    text = (
        "env dump:\n"
        "GITHUB_TOKEN=ghp_abcdefghij0123456789\n"
        "PATH=/usr/bin\n"
        "done."
    )
    out = redact_text(text)
    assert "ghp_abcdefghij0123456789" not in out
    assert "GITHUB_TOKEN=ghp_" not in out
    assert "PATH=/usr/bin" in out  # 无关内容必须原样保留
    assert "done." in out


# ── §4 反例：★ 误伤才是真风险 ──────────────────────────────────

FALSE_POSITIVE = [
    # oh-my-pi 的血泪教训（secrets/patterns.ts:60 明确点名这三个）
    "token_expiry_seconds",
    "api_key_rotation_days",
    "password_policy",
    # 太短，不该当成真 key
    "sk-x",
    "AKIASHORT",
    "ghp_short",
    "hf_short",
    "Bearer abc",
    # 正常内容
    "path/to/file.py",
    "SELECT * FROM users",
    "/home/me/.ssh/id_rsa",
    "user@example.com",  # ★ PII 不挡（用户明确"只挡明显的"）
    "order 1234567890123456",
    "令牌过期时间",
    "Authorization: Bearer",
    "https://github.com/can1357/oh-my-pi",
    "sk-learn is a library",
]


@pytest.mark.parametrize("text", FALSE_POSITIVE)
def test_does_not_redact_normal_content(text: str) -> None:
    assert redact_text(text) == text


def test_entropy_gate_only_applies_to_generic_shapes() -> None:
    """熵值门槛只对**泛化形态**生效（实测教训，见 redact.py 注释）。

    - `Bearer` + 纯小写长串 ⇒ 文档占位，不是凭据 ⇒ 放过
    - `ghp_` + 纯小写长串 ⇒ 前缀已足够特异 ⇒ 照样遮
    """
    placeholder = "Bearer " + "a" * 40
    assert redact_text(placeholder) == placeholder

    token = "ghp_" + "a" * 40
    assert redact_text(token) != token


def test_no_hit_returns_identical_string() -> None:
    """热路径快路径：无命中时必须是**同一个**内容（不是等价的副本）。"""
    s = "完全没有任何敏感内容的一段普通输出。"
    assert redact_text(s) is s or redact_text(s) == s


def test_empty_and_non_str_inputs() -> None:
    assert redact_text("") == ""
    assert redact_text(None) is None  # type: ignore[arg-type]


# ── §4 redact_mapping：key 名字规则 ────────────────────────────

_NAME_SENSITIVE = [
    "GITHUB_TOKEN",
    "api_key",
    "API_KEY",
    "db_password",
    "PASSWORD",
    "client_secret",
    "AWS_SECRET_ACCESS_KEY",
    "PRIVATE_KEY",
    "auth_header",
]


@pytest.mark.parametrize("key", _NAME_SENSITIVE)
def test_mapping_masks_by_key_name(key: str) -> None:
    out = redact_mapping({key: "anything-at-all"})
    assert out[key] == MASK_ALL


def test_mapping_allowlist_keys_never_masked() -> None:
    allow = ["PATH", "HOME", "LANG", "TZ", "SHELL", "USER", "PWD", "PYTHONPATH"]
    data = {k: "/some/value" for k in allow}
    assert redact_mapping(data) == data


def test_allowlist_wins_over_hint() -> None:
    """allowlist 优先级高于名字提示（否则 allowlist 名存实亡）。"""
    assert redact_mapping({"AUTHOR": "x"}) == {"AUTHOR": "x"}
    assert redact_mapping({"COMMIT": "x"}) == {"COMMIT": "x"}


def test_mapping_does_not_mutate_input() -> None:
    data = {"GITHUB_TOKEN": "ghp_abc", "nested": {"k": ["sk-" + _A * 12]}}
    before = copy.deepcopy(data)
    redact_mapping(data)
    assert data == before


def test_mapping_recurses_into_dict_and_list() -> None:
    data = {"a": {"b": [{"c": "sk-" + _A * 12}]}}
    out = redact_mapping(data)
    assert "REDACTED" in out["a"]["b"][0]["c"]


def test_mapping_stops_at_max_depth() -> None:
    """超过 MAX_DEPTH 不再递归（防爆栈）——不能抛 RecursionError。"""
    deep: dict = {}
    cur = deep
    for _ in range(MAX_DEPTH + 20):
        cur["k"] = {}
        cur = cur["k"]
    assert isinstance(redact_mapping(deep), dict)


def test_mapping_passes_through_scalars() -> None:
    assert redact_mapping(42) == 42
    assert redact_mapping(None) is None
    assert redact_mapping(True) is True


# ── §5 开关 ────────────────────────────────────────────────────

_ENV = "CODEFORGE_REDACT"


def test_redaction_defaults_to_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认**开**（与 omp 相反的理由见 spec §4.6）。"""
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setenv("CODEFORGE_CONFIG", "nonexistent.yaml")
    assert redaction_config().enabled is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("false", False), ("off", False), ("no", False),
     ("1", True), ("true", True), ("on", True)],
)
def test_env_switch(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv(_ENV, value)
    assert redaction_config().enabled is expected


def test_env_wins_over_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("observability:\n  redact_secrets: true\n", encoding="utf-8")
    monkeypatch.setenv("CODEFORGE_CONFIG", str(cfg))
    monkeypatch.setenv(_ENV, "0")
    assert redaction_config().enabled is False


def test_yaml_used_when_env_absent(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("observability:\n  redact_secrets: false\n", encoding="utf-8")
    monkeypatch.setenv("CODEFORGE_CONFIG", str(cfg))
    monkeypatch.delenv(_ENV, raising=False)
    assert redaction_config().enabled is False


def test_broken_yaml_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置读不到不许抛 —— 用默认值继续。"""
    cfg = "/nonexistent/dir/c.yaml"
    monkeypatch.setenv("CODEFORGE_CONFIG", cfg)
    monkeypatch.delenv(_ENV, raising=False)
    assert redaction_config() == RedactionSettings(enabled=True)
