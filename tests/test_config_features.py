"""config features 解析单测（T25）。

覆盖：load_config_full 解析 features 段；coordinator.is_enabled 兼容 FeaturesConfig 与 config 对象。
"""

from __future__ import annotations

from config.loader import load_config_full
from config.model import FeaturesConfig
from core.coordinator import is_enabled


def test_load_config_full_parses_features(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  - name: Anthropic\n"
        "    protocol: anthropic\n"
        "    model: claude\n"
        "    api_key: sk-x\n"
        "features:\n"
        "  coordinator_mode: true\n"
        "  fork_teammate: true\n",
        encoding="utf-8",
    )
    providers, features = load_config_full(str(cfg))
    assert len(providers) == 1
    assert features.coordinator_mode is True
    assert features.fork_teammate is True


def test_load_config_full_defaults_when_no_features(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  - name: Anthropic\n"
        "    protocol: anthropic\n"
        "    model: claude\n"
        "    api_key: sk-x\n",
        encoding="utf-8",
    )
    _, features = load_config_full(str(cfg))
    assert features.coordinator_mode is False
    assert features.fork_teammate is False


def test_coordinator_accepts_features_config(monkeypatch):
    monkeypatch.setenv("CODEFORGE_COORDINATOR_MODE", "1")
    features = FeaturesConfig(coordinator_mode=True)
    assert is_enabled(features) is True
    monkeypatch.delenv("CODEFORGE_COORDINATOR_MODE", raising=False)
    assert is_enabled(features) is False


def test_coordinator_accepts_config_object(monkeypatch):
    class _Cfg:
        features = FeaturesConfig(coordinator_mode=True)

    monkeypatch.setenv("CODEFORGE_COORDINATOR_MODE", "1")
    assert is_enabled(_Cfg()) is True


# ── features.host（任务 15）────────────────────────────────────────────
#
# 解析原则：非法值告警并退回默认，**不阻断启动** —— 配置写错的代价不该是
# "起不来"，而 host 档位本身有安全默认（deny_all）。


def _cfg_with_host(tmp_path, host_block: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  - name: Anthropic\n"
        "    protocol: anthropic\n"
        "    model: claude\n"
        "    api_key: sk-x\n"
        "features:\n"
        "  host:\n" + host_block,
        encoding="utf-8",
    )
    return str(cfg)


def test_host_absent_means_none(tmp_path):
    """没写 features.host → None（= 未配置，任务 16 据此判断是否走 host 路径）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  - name: Anthropic\n"
        "    protocol: anthropic\n"
        "    model: claude\n"
        "    api_key: sk-x\n"
        "features:\n"
        "  coordinator_mode: false\n",
        encoding="utf-8",
    )
    _, features = load_config_full(str(cfg))
    assert features.host is None


def test_host_parsed_with_defaults(tmp_path):
    """只写 enabled 时，其余字段走安全默认。"""
    _, features = load_config_full(
        _cfg_with_host(tmp_path, "    enabled: true\n")
    )
    assert features.host is not None
    assert features.host.enabled is True
    assert features.host.port == 0
    assert features.host.token_file == ""
    assert features.host.unattended_policy == "deny_all"


def test_host_parsed_explicit_values(tmp_path):
    _, features = load_config_full(
        _cfg_with_host(
            tmp_path,
            "    enabled: true\n"
            "    port: 18080\n"
            "    token_file: /tmp/tok\n"
            "    unattended_policy: allow_write\n",
        )
    )
    h = features.host
    assert (h.enabled, h.port, h.token_file, h.unattended_policy) == (
        True,
        18080,
        "/tmp/tok",
        "allow_write",
    )


def test_host_invalid_policy_falls_back_to_deny_all(tmp_path, capsys):
    """非法档位 → 告警 + 退回 deny_all（尤其不能猜成更宽的档）。"""
    _, features = load_config_full(
        _cfg_with_host(tmp_path, "    unattended_policy: allow_everything\n")
    )
    assert features.host.unattended_policy == "deny_all"
    assert "不是有效档位" in capsys.readouterr().err


def test_host_rejects_removed_readonly_policy(tmp_path, capsys):
    """`allow_readonly` 是被刻意移除的冗余档，配了也要退回默认。"""
    _, features = load_config_full(
        _cfg_with_host(tmp_path, "    unattended_policy: allow_readonly\n")
    )
    assert features.host.unattended_policy == "deny_all"
    assert "不是有效档位" in capsys.readouterr().err


def test_host_invalid_port_falls_back_to_random(tmp_path, capsys):
    _, features = load_config_full(_cfg_with_host(tmp_path, "    port: not-a-port\n"))
    assert features.host.port == 0
    assert "不是整数" in capsys.readouterr().err


def test_host_out_of_range_port_falls_back_to_random(tmp_path, capsys):
    _, features = load_config_full(_cfg_with_host(tmp_path, "    port: 70000\n"))
    assert features.host.port == 0
    assert "超出" in capsys.readouterr().err


def test_host_accepts_every_valid_policy(tmp_path):
    """三个合法档位都要能原样读出来（防止校验集合写错）。"""
    for policy in ("allow_all", "allow_write", "deny_all"):
        _, features = load_config_full(
            _cfg_with_host(tmp_path, f"    unattended_policy: {policy}\n")
        )
        assert features.host.unattended_policy == policy
