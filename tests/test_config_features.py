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
