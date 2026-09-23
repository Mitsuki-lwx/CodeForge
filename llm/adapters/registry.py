"""协议/厂商适配器的注册表。

**为什么要这个模块**：原先"protocol + vendor → Adapter 类"的选择逻辑写死在
`llm/protocol.py` 的一个 `if/elif` 链里，于是

  1. 加一个协议/厂商就要改那个中心函数；
  2. 厂商识别靠**字符串猜测**（`"deepseek" in base_url`、`model.startswith(...)`），
     端点改名就失灵；
  3. 返回类型是 `type[Adapter]`，提供方**带不了自己的配置**。

改成注册表后：**加协议 = 加一个实现模块 + 一个装饰器**，本文件与
`protocol.py` / `client.py` 都不用动。

**为什么是模块级全局**（而不是挂在某个 context 上）：
适配器选择发生在 `LLMClient.create()`，属于 bootstrap 之前 —— 那时没有任何
context 可用。上游 dsh 能把注册挂到 `ctx` 上，是因为它整个运行时就是 Cordis；
我们若为了挂注册表而引入一个 context，是本末倒置。

代价：模块级全局 + **import 即注册** → 实现模块必须被 import。
唯一的加载入口是 `llm/adapters/__init__.py`（它已经 import 了全部实现）。
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免运行时循环 import
    from llm.adapters.base import Adapter


@dataclass(frozen=True)
class AdapterRegistration:
    """一条注册规则：某协议（可限定厂商）由哪个 Adapter 类实现。"""

    protocol: str  # 严格匹配（与改动前一致）；约定一律写小写
    vendor: str | None  # None = 该协议下任意厂商；匹配时大小写不敏感
    adapter_cls: type  # Adapter 子类
    client: str | None  # "llm.anthropic_client.AnthropicClient"（延迟 import）
    predicate: Callable[[str, str | None, str, str | None], bool] | None
    priority: int  # 大者优先
    source: str  # "模块.类名"，重复注册时报错用


_REGISTRY: list[AdapterRegistration] = []
_CLIENT_CACHE: dict[str, type] = {}
# 兜底告警去重：(protocol, vendor) → 已提示过
_WARNED: set[tuple[str, str | None]] = set()


def register_adapter(
    protocol: str,
    *,
    vendor: str | None = None,
    predicate: Callable[[str, str | None, str, str | None], bool] | None = None,
    client: str | None = None,
    priority: int = 0,
) -> Callable[[type], type]:
    """把一个 Adapter 类登记为某协议/厂商的实现（装饰器，可叠加）。

    匹配语义（按 priority 降序，同级按注册序）：

      - ``protocol`` **严格比较**（改动前就是 `protocol == "openai"` 这种严格判断，
        这里刻意不做小写化，以免悄悄改变既有配置的选择结果）；约定一律写小写。
      - 声明了 ``vendor`` → 要求 ``vendor`` 参数（**大小写不敏感**，与改动前的
        `vendor.lower()` 一致）精确相等；
      - 声明了 ``predicate`` → 要求谓词返回真；
      - 两者都没声明 → **该协议的默认实现**（恒命中）。

    ``client`` 是该协议用哪个 LLMClient 实现驱动的**全限定名**
    （如 ``"llm.anthropic_client.AnthropicClient"``）。用字符串而不是类对象，
    是因为注册发生在适配器模块 import 时，而 ``llm/adapters/*`` 与
    ``llm/client.py`` 存在反向依赖，直接引用会形成循环 import。

    同一个类叠加多条注册是**合法**的（例如"厂商精确"+"端点启发式"两条规则）；
    但同一 ``(protocol, vendor)`` 上登记**不同类**属于手滑，直接抛错。
    """
    proto = protocol or ""  # 严格比较，见 docstring
    vend = vendor.lower() if vendor else None

    def deco(cls: type) -> type:
        source = f"{cls.__module__}.{cls.__qualname__}"
        for existing in _REGISTRY:
            if (
                existing.protocol == proto
                and existing.vendor == vend
                and existing.adapter_cls is not cls
                and existing.predicate is None
                and predicate is None
            ):
                raise ValueError(
                    f"协议 {proto!r}（vendor={vend!r}）已被 "
                    f"{existing.source} 注册，不能再用 {source} 重复注册"
                )
        _REGISTRY.append(
            AdapterRegistration(
                protocol=proto,
                vendor=vend,
                adapter_cls=cls,
                client=client,
                predicate=predicate,
                priority=priority,
                source=source,
            )
        )
        return cls

    return deco


def _matches(
    reg: AdapterRegistration,
    protocol: str,
    vendor: str | None,
    model: str,
    base_url: str | None,
) -> bool:
    if reg.protocol != protocol:
        return False
    if reg.vendor is not None:
        return (vendor or "").lower() == reg.vendor
    if reg.predicate is not None:
        return bool(reg.predicate(protocol, vendor, model, base_url))
    return True  # 协议默认实现


def _ordered() -> list[AdapterRegistration]:
    """按 priority 降序；同级保持注册序（python sorted 稳定，与原 if/elif 的
    "先写先命中"一致）。"""
    return sorted(_REGISTRY, key=lambda r: -r.priority)


def _load_client(path: str) -> type:
    cached = _CLIENT_CACHE.get(path)
    if cached is not None:
        return cached
    module_name, _, attr = path.rpartition(".")
    if not module_name:
        raise ValueError(f"client 必须是全限定名（如 'pkg.mod.Class'），收到 {path!r}")
    cls = getattr(importlib.import_module(module_name), attr)
    _CLIENT_CACHE[path] = cls
    return cls


def _warn_unknown(protocol: str, vendor: str | None, fallback: type) -> None:
    key = (protocol, vendor)
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(
        f"[llm] 未识别的 protocol/vendor（protocol={protocol!r}, vendor={vendor!r}），"
        f"回退到 {fallback.__name__}；如需支持请在 llm/adapters/ 下注册",
        file=sys.stderr,
    )


def resolve_adapter_class(
    protocol: str,
    vendor: str | None = None,
    model: str = "",
    base_url: str | None = None,
) -> type[Adapter]:
    """按协议/厂商/模型/端点解析出 Adapter 类。

    protocol 是 wire 格式的一级判别；vendor 只在同一协议族内做细化
    （例如 anthropic 协议下 vendor=deepseek 无意义，仍走 AnthropicAdapter）。
    """
    proto = protocol or ""  # 严格比较（与改动前一致）
    for reg in _ordered():
        if _matches(reg, proto, vendor, model, base_url):
            return reg.adapter_cls

    fallback = _global_fallback()
    _warn_unknown(proto, vendor, fallback)
    return fallback


def _global_fallback() -> type[Adapter]:
    """全不命中时的兜底：openai 协议的默认实现（与改动前行为一致）。"""
    cands = [
        r
        for r in _REGISTRY
        if r.protocol == "openai" and r.vendor is None and r.predicate is None
    ]
    if cands:
        return min(cands, key=lambda r: r.priority).adapter_cls
    # 注册表为空（实现模块未被 import）也别崩：直接取 openai base 的默认实现。
    from llm.adapters.openai_base import OpenAIConversationAdapter

    return OpenAIConversationAdapter


def resolve_client(adapter_cls: type) -> type:
    """由注册项声明的 client 全限定名载入对应的 LLMClient 实现。

    取代原先 `adapter_cls.__name__.startswith("Anthropic")` 这种按**类名字符串**
    判断客户端的做法（加协议就得改 `client.py`，而且改个类名就失灵）。
    """
    for reg in _REGISTRY:
        if reg.adapter_cls is adapter_cls and reg.client:
            return _load_client(reg.client)
    from llm.openai_client import OpenAIClient

    return OpenAIClient


def iter_registrations() -> Iterator[AdapterRegistration]:
    """只读遍历当前注册项（测试与诊断用）。"""
    return iter(tuple(_REGISTRY))


def _unregister_all() -> list[AdapterRegistration]:
    """清空注册表并返回快照（仅供测试做备份/复原）。"""
    snapshot = list(_REGISTRY)
    _REGISTRY.clear()
    _CLIENT_CACHE.clear()
    _WARNED.clear()
    return snapshot


def _restore(registrations: list[AdapterRegistration]) -> None:
    """把 `_unregister_all()` 的快照装回去（仅供测试）。"""
    _REGISTRY[:] = list(registrations)
    _CLIENT_CACHE.clear()
    _WARNED.clear()


__all__ = [
    "AdapterRegistration",
    "iter_registrations",
    "register_adapter",
    "resolve_adapter_class",
    "resolve_client",
]
