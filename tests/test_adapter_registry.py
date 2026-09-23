"""协议适配器注册表（`llm.adapters.registry`）单元测试。

关注三件事：

1. **行为等价** —— 重构前后"protocol + vendor + model + base_url → Adapter 类"
   的选择结果**逐字一致**（`tests/test_protocol.py` 是遗留回归网，本文件是它的
   等价表版本 + 新能力）；
2. **可扩展** —— 新增协议只靠一个装饰器，`protocol.py` / `client.py` 零改动；
3. **健壮** —— 重复注册报错、未知协议兜底且告警、客户端解析是真类相等。
"""

from __future__ import annotations

import pytest

from llm.adapters import (
    AnthropicAdapter,
    DeepSeekConversationAdapter,
    OpenAIConversationAdapter,
)
from llm.adapters.registry import (
    iter_registrations,
    register_adapter,
    resolve_adapter_class,
    resolve_client,
)


@pytest.fixture
def clean_registry():
    """备份/还原全局注册表。

    注册表刻意是**模块级全局**（理由见 `registry.py` 顶部：适配器选择发生在
    bootstrap 之前，那时没有 context 可挂）。代价是测试之间会串味，所以需要
    这个 fixture 做隔离。
    """
    from llm.adapters import registry

    snapshot = registry._unregister_all()
    try:
        yield registry
    finally:
        registry._restore(snapshot)


# ── 1. 等价表：改动前后必须逐字一致 ─────────────────────────────────


@pytest.mark.parametrize(
    ("protocol", "vendor", "model", "base_url", "expected"),
    [
        # anthropic 协议：vendor / model / host 都不改变选择（vendor 不改 wire）
        ("anthropic", "deepseek", "deepseek-x", None, AnthropicAdapter),
        ("anthropic", "anthropic", "claude", None, AnthropicAdapter),
        ("anthropic", None, "claude", None, AnthropicAdapter),
        ("anthropic", "notarealvendor", "claude", None, AnthropicAdapter),
        # openai 协议：vendor 精确匹配
        (
            "openai",
            "deepseek",
            "some-model",
            "https://x.example.com",
            DeepSeekConversationAdapter,
        ),
        # openai 协议：vendor 明说 openai → 官方适配器
        ("openai", "openai", "gpt-4", None, OpenAIConversationAdapter),
        # openai 协议：vendor 缺省 + 模型名像 deepseek
        ("openai", None, "deepseek-x", None, DeepSeekConversationAdapter),
        # openai 协议：vendor 缺省 + 端点像 deepseek
        (
            "openai",
            None,
            "gpt-4",
            "https://api.deepseek.com",
            DeepSeekConversationAdapter,
        ),
        # 未知 vendor **不短路** —— 继续往下看 host/model（旧实现的关键语义）
        ("openai", "notarealvendor", "gpt-4", None, OpenAIConversationAdapter),
        ("openai", "notarealvendor", "deepseek-x", None, DeepSeekConversationAdapter),
        # 未知 protocol → 兜底 openai 默认实现
        ("weird", None, "x", None, OpenAIConversationAdapter),
    ],
)
def test_selection_matches_legacy_behaviour(
    protocol, vendor, model, base_url, expected
):
    assert resolve_adapter_class(protocol, vendor, model, base_url) is expected


def test_protocol_is_strict_but_vendor_is_case_insensitive():
    """protocol 严格比较（与改动前的 `protocol == "openai"` 一致）；
    vendor 大小写不敏感（与改动前的 `vendor.lower()` 一致）。

    这条明确钉住"不做小写化"的决定 —— 若哪天有人顺手把 protocol 也
    lower 了，这个用例会失败，提醒他那是**行为变化**而非无害重构。
    """
    assert (
        resolve_adapter_class("openai", "DeepSeek", "m", None)
        is DeepSeekConversationAdapter
    )
    # 大写 protocol 走兜底，与旧实现相同（不是 AnthropicAdapter）
    assert (
        resolve_adapter_class("ANTHROPIC", None, "m", None) is OpenAIConversationAdapter
    )


# ── 2. 注册表完整性（防 import 漏注册）────────────────────────────


def test_all_builtin_implementations_are_registered():
    """三个内置实现都必须出现在注册表里 —— 防"实现模块没被 import"这类静默失效。"""
    registered = {r.adapter_cls for r in iter_registrations()}
    assert AnthropicAdapter in registered
    assert DeepSeekConversationAdapter in registered
    assert OpenAIConversationAdapter in registered


def test_registrations_declare_a_client():
    """内建注册项都要声明 client，否则 `resolve_client` 会静默回落。"""
    for reg in iter_registrations():
        assert reg.client, f"{reg.source} 没声明 client"


def test_deepseek_rules_outrank_the_protocol_default():
    """DeepSeek 的两条规则（精确 900 / 启发式 800）必须都高于 openai 默认项(0)。

    否则"像 deepseek"的端点会被默认项先命中 —— 这是排序最容易写错的地方。
    """
    regs = [
        r for r in iter_registrations() if r.adapter_cls is DeepSeekConversationAdapter
    ]
    defaults = [
        r
        for r in iter_registrations()
        if r.protocol == "openai" and r.vendor is None and r.predicate is None
    ]
    assert len(regs) == 2, "DeepSeek 应有两条规则（vendor 精确 + 端点启发式）"
    assert defaults, "应有 openai 的默认注册项"
    assert min(r.priority for r in regs) > max(r.priority for r in defaults)


# ── 3. 客户端解析（取代类名前缀判断）────────────────────────────


def test_resolve_client_returns_exact_class():
    from llm.anthropic_client import AnthropicClient
    from llm.openai_client import OpenAIClient

    assert resolve_client(AnthropicAdapter) is AnthropicClient
    assert resolve_client(DeepSeekConversationAdapter) is OpenAIClient
    assert resolve_client(OpenAIConversationAdapter) is OpenAIClient


def test_resolve_client_falls_back_for_unregistered_class(clean_registry):
    """没在注册表里的类 → 回落 OpenAIClient（与旧的 else 兜底一致），不抛错。"""
    from llm.openai_client import OpenAIClient

    class Nobody:
        pass

    assert resolve_client(Nobody) is OpenAIClient


# ── 4. 未知协议：兜底 + 告警（且告警不影响选择）────────────────────


def test_unknown_protocol_warns_but_still_falls_back(clean_registry, capsys):
    """未知 protocol 的选择结果与改动前一致（仍兜底），只是多一条告警。

    注册表被清空时也不能崩 —— 兜底会直接从 openai_base 取默认实现。
    """
    got = resolve_adapter_class("totally-unknown", None, "m", None)
    assert got is OpenAIConversationAdapter

    err = capsys.readouterr().err
    assert "totally-unknown" in err
    assert "OpenAIConversationAdapter" in err


def test_unknown_protocol_warning_is_deduplicated(clean_registry, capsys):
    """同一种未知协议只提示一次 —— 否则每个请求刷一条会淹掉日志。"""
    for _ in range(4):
        resolve_adapter_class("dup-proto", None, "m", None)
    err = capsys.readouterr().err
    assert err.count("dup-proto") == 1


def test_known_protocol_never_warns(capsys):
    """正常路径不该产生告警（别把 info 级别的事做成噪音）。"""
    resolve_adapter_class("openai", "deepseek", "m", None)
    resolve_adapter_class("anthropic", None, "m", None)
    assert capsys.readouterr().err == ""


# ── 5. 可扩展性：加协议只需一个装饰器 ──────────────────────────────


def test_new_protocol_needs_only_a_decorator(clean_registry):
    """新增一个协议 = 一个类 + 一个装饰器；`protocol.py` / `client.py` 零改动。

    这里显式调用 `register_adapter(...)`（测试里没有独立实现模块可供 import），
    语义与写在实现模块顶部的 `@register_adapter(...)` 完全相同。
    """

    class AcmeAdapter(OpenAIConversationAdapter):
        """假想的第三家上游。"""

    register_adapter("acme", priority=100, client="llm.openai_client.OpenAIClient")(
        AcmeAdapter
    )

    assert resolve_adapter_class("acme", None, "m", None) is AcmeAdapter
    # 协议默认项忽略 vendor
    assert resolve_adapter_class("acme", "whatever", "m", None) is AcmeAdapter
    assert resolve_client(AcmeAdapter).__name__ == "OpenAIClient"


def test_same_class_can_register_twice(clean_registry):
    """同一个类叠两条规则（精确 + 谓词）是**合法**的，不是重复注册。"""

    class MultiAdapter(OpenAIConversationAdapter):
        pass

    register_adapter("multi", vendor="x", priority=900)(MultiAdapter)
    register_adapter("multi", predicate=lambda *a: True, priority=800)(MultiAdapter)

    regs = [r for r in iter_registrations() if r.adapter_cls is MultiAdapter]
    assert len(regs) == 2
    assert resolve_adapter_class("multi", "x", "m", None) is MultiAdapter
    assert resolve_adapter_class("multi", "y", "m", None) is MultiAdapter  # 谓词恒真


# ── 6. 重复注册不同类 → 早暴露 ────────────────────────────────────


def test_duplicate_registration_of_different_classes_raises(clean_registry):
    """同一 (protocol, vendor) 上登记不同类 —— 手滑，必须抛错。"""

    class FirstAdapter(OpenAIConversationAdapter):
        pass

    class SecondAdapter(OpenAIConversationAdapter):
        pass

    register_adapter("dup", vendor="v")(FirstAdapter)

    with pytest.raises(ValueError) as ei:
        register_adapter("dup", vendor="v")(SecondAdapter)

    msg = str(ei.value)
    assert "FirstAdapter" in msg and "SecondAdapter" in msg, "错误信息要能定位到两个类"
