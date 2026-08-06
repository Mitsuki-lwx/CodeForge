"""配置模块测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from config.model import ProviderConfig
from config.loader import load_config, _validate_providers, VALID_PROTOCOLS


class TestProviderConfig:
    """ProviderConfig dataclass 基本行为。"""

    def test_minimal_config(self):
        cfg = ProviderConfig(
            name="test",
            protocol="anthropic",
            model="claude-3",
            api_key="sk-test",
        )
        assert cfg.name == "test"
        assert cfg.protocol == "anthropic"
        assert cfg.model == "claude-3"
        assert cfg.api_key == "sk-test"
        assert cfg.base_url is None
        assert cfg.thinking is False

    def test_full_config(self):
        cfg = ProviderConfig(
            name="test",
            protocol="openai",
            model="gpt-4",
            api_key="sk-test",
            base_url="https://custom.example.com/v1",
            thinking=True,
        )
        assert cfg.base_url == "https://custom.example.com/v1"
        assert cfg.thinking is True

    def test_empty_base_url_normalized(self):
        cfg = ProviderConfig(
            name="test", protocol="anthropic", model="c", api_key="k",
        )
        assert cfg.base_url is None


class TestValidateProviders:
    """Provider 列表校验。"""

    def test_valid_single(self):
        providers = _validate_providers([{
            "name": "A",
            "protocol": "anthropic",
            "model": "c",
            "api_key": "k",
        }])
        assert len(providers) == 1
        assert providers[0].name == "A"

    def test_valid_multiple(self):
        providers = _validate_providers([
            {"name": "A", "protocol": "anthropic", "model": "c", "api_key": "k"},
            {"name": "B", "protocol": "openai", "model": "g", "api_key": "k2"},
        ])
        assert len(providers) == 2

    def test_empty_list_exits(self):
        with pytest.raises(SystemExit):
            _validate_providers([])

    def test_missing_name_exits(self):
        with pytest.raises(SystemExit):
            _validate_providers([{
                "protocol": "anthropic", "model": "c", "api_key": "k",
            }])

    def test_empty_api_key_exits(self):
        with pytest.raises(SystemExit):
            _validate_providers([{
                "name": "A", "protocol": "anthropic", "model": "c", "api_key": "",
            }])

    def test_invalid_protocol_exits(self):
        with pytest.raises(SystemExit):
            _validate_providers([{
                "name": "A", "protocol": "invalid", "model": "c", "api_key": "k",
            }])

    def test_missing_model_exits(self):
        with pytest.raises(SystemExit):
            _validate_providers([{
                "name": "A", "protocol": "anthropic", "api_key": "k",
            }])


class TestLoadConfig:
    """配置文件加载。"""

    def test_load_valid_yaml(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({
            "providers": [
                {"name": "A", "protocol": "anthropic", "model": "c", "api_key": "k"},
            ]
        }))
        providers = load_config(str(cfg))
        assert len(providers) == 1

    def test_file_not_found_exits(self):
        with pytest.raises(SystemExit):
            load_config("/nonexistent/config.yaml")

    def test_invalid_yaml_exits(self, tmp_path: Path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text("{bad yaml: [")
        with pytest.raises(SystemExit):
            load_config(str(cfg))

    def test_not_dict_top_level_exits(self, tmp_path: Path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text(yaml.dump(["just a list"]))
        with pytest.raises(SystemExit):
            load_config(str(cfg))

    def test_providers_not_list_exits(self, tmp_path: Path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text(yaml.dump({"providers": "not a list"}))
        with pytest.raises(SystemExit):
            load_config(str(cfg))
