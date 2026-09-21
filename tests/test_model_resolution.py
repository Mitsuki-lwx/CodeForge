"""模型名解析测试（spec_model_resolution）。

三层一起测，因为它们共同构成一条链：
  1. **config 层** —— `model_aliases` 的解析与宽容处理（loader）
  2. **解析层** —— `resolve_model_name` 的决策表（纯函数）
  3. **接入层** —— `LLMClient.create_with_model` 产出的模型名**真正生效**

背景（实测）：内置 `explore.md` 声明 `model: haiku`，而项目里没有任何
"别名 → 实际模型"映射表，`haiku` 被当模型名发给 API → `model is not found`。
旧的角色层白名单还反着来：写**具体模型名**会被静默换成 `inherit`。
"""

from __future__ import annotations

import pytest

import llm.client as llm_client
from config.loader import _parse_model_aliases
from config.model import ProviderConfig
from llm.client import RESERVED_MODEL_ALIASES, LLMClient, resolve_model_name


def _cfg(**kw) -> ProviderConfig:
    base = {
        "name": "t",
        "protocol": "openai",
        "model": "main-model",
        "api_key": "sk-x",
    }
    base.update(kw)
    return ProviderConfig(**base)


# ── 1. config 层：model_aliases 解析 ──


class TestParseModelAliases:
    def test_absent_returns_empty(self):
        assert _parse_model_aliases({}, 0) == {}

    def test_valid_mapping(self):
        got = _parse_model_aliases({"model_aliases": {"haiku": "cheap"}}, 0)
        assert got == {"haiku": "cheap"}

    @pytest.mark.parametrize("bad", [["haiku"], "haiku", 42])
    def test_non_mapping_ignored_entirely(self, bad):
        assert _parse_model_aliases({"model_aliases": bad}, 0) == {}

    def test_non_string_target_skipped_others_kept(self):
        """一项写错只丢那一项，不牵连其它 —— 与 vendor / tier 的宽容姿势一致。"""
        got = _parse_model_aliases(
            {"model_aliases": {"haiku": 123, "sonnet": "mid"}}, 0
        )
        assert got == {"sonnet": "mid"}

    def test_alias_key_lowercased(self):
        """别名统一小写：角色解析侧会把 model 名 lower()，两边必须对得上。"""
        got = _parse_model_aliases({"model_aliases": {"HAIKU": "cheap"}}, 0)
        assert got == {"haiku": "cheap"}

    @pytest.mark.parametrize("alias", ["", "   "])
    def test_blank_alias_skipped(self, alias):
        assert _parse_model_aliases({"model_aliases": {alias: "x"}}, 0) == {}

    def test_blank_target_skipped(self):
        assert _parse_model_aliases({"model_aliases": {"haiku": "  "}}, 0) == {}


# ── 2. 解析层：决策表 ──


class TestResolveModelName:
    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_inherits_main_model(self, value):
        assert resolve_model_name(_cfg(), value) == "main-model"

    @pytest.mark.parametrize("value", ["inherit", "INHERIT", "Inherit"])
    def test_inherit_keyword_case_insensitive(self, value):
        assert resolve_model_name(_cfg(), value) == "main-model"

    def test_alias_mapped_to_target(self):
        cfg = _cfg(model_aliases={"haiku": "cheap-model"})
        assert resolve_model_name(cfg, "haiku") == "cheap-model"

    def test_alias_lookup_case_insensitive(self):
        cfg = _cfg(model_aliases={"haiku": "cheap-model"})
        assert resolve_model_name(cfg, "HAIKU") == "cheap-model"

    @pytest.mark.parametrize("alias", ["haiku", "sonnet", "opus"])
    def test_reserved_alias_without_mapping_falls_back_to_main(self, alias):
        """保留别名没配映射 → 退回主模型。

        这是本次修的核心：**不能**把 `haiku` 原样发出去（必 404），
        也不能默默改掉用户意图而不说 —— 所以退回主模型 + 打印告警。
        """
        llm_client._warned_once.clear()
        assert resolve_model_name(_cfg(), alias) == "main-model"

    def test_reserved_alias_without_mapping_warns(self, capsys):
        llm_client._warned_once.clear()
        resolve_model_name(_cfg(name="warn-provider"), "haiku")
        err = capsys.readouterr().err
        assert "model_aliases" in err
        assert "warn-provider" in err

    def test_warning_emitted_only_once_per_provider_and_model(self, capsys):
        """同一个 (provider, model) 只提示一次 —— 否则派 N 个子 Agent 会刷 N 条。"""
        llm_client._warned_once.clear()
        cfg = _cfg(name="dedup-provider")
        for _ in range(3):
            resolve_model_name(cfg, "haiku")
        err = capsys.readouterr().err
        # 数**告警条数**而不是关键词出现次数：告警文案里 `model_aliases` 本身
        # 就要出现两次（一次说缺哪个键、一次说该配成什么样）。
        assert err.count("是模型别名，但 provider") == 1

    @pytest.mark.parametrize(
        "name",
        [
            "kimi-k3",
            "glm-5.2",
            "deepseek-v4-flash",
            "sensenova-6.8-flash-lite",
            "deepseek-v4-pro",
        ],
    )
    def test_concrete_model_name_passthrough(self, name):
        """具体模型名原样使用 —— 商汤端点上实测可用的名字都要能直传。"""
        assert resolve_model_name(_cfg(), name) == name

    @pytest.mark.parametrize("bad", ["bad name", "a;b", "a\nb", "-leading"])
    def test_invalid_chars_fall_back(self, bad):
        llm_client._warned_once.clear()
        assert resolve_model_name(_cfg(), bad) == "main-model"

    def test_is_pure(self):
        """纯函数：不改动传入的 config（重复调用结果一致）。"""
        cfg = _cfg(model_aliases={"haiku": "cheap-model"})
        snapshot = dict(cfg.model_aliases)
        for _ in range(2):
            assert resolve_model_name(cfg, "haiku") == "cheap-model"
        assert cfg.model == "main-model"
        assert cfg.model_aliases == snapshot

    def test_reserved_aliases_are_the_three_semantic_tiers(self):
        assert RESERVED_MODEL_ALIASES == {"haiku", "sonnet", "opus"}


# ── 3. 接入层：真的被用上 ──


class TestCreateWithModel:
    def test_client_gets_resolved_model(self):
        cfg = _cfg(model_aliases={"haiku": "cheap-model"})
        assert LLMClient.create_with_model(cfg, "haiku").config.model == "cheap-model"

    def test_alias_without_mapping_yields_main_model(self):
        llm_client._warned_once.clear()
        assert LLMClient.create_with_model(_cfg(), "haiku").config.model == "main-model"

    def test_concrete_name_reaches_client(self):
        client = LLMClient.create_with_model(_cfg(), "kimi-k3")
        assert client.config.model == "kimi-k3"

    def test_original_config_untouched(self):
        """覆盖模型不该改到调用方持有的 provider 配置（否则会串味）。"""
        cfg = _cfg(model_aliases={"haiku": "cheap-model"})
        LLMClient.create_with_model(cfg, "haiku")
        assert cfg.model == "main-model"

    def test_other_fields_preserved(self):
        cfg = _cfg(base_url="https://example.com/v1", vendor="deepseek")
        client = LLMClient.create_with_model(cfg, "kimi-k3")
        assert client.config.base_url == "https://example.com/v1"
        assert client.config.vendor == "deepseek"
