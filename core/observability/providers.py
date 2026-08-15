"""Observability 门面 + 进程内指标快照。

外部代码统一 `from core.observability.providers import get_tracer, get_meter,
get_logger, record_metric, snapshot`。

sdk.py 初始化后调用 `install(...)` 注入真实 provider;未初始化或未启用时,
get_tracer/get_meter/get_logger 返回 no-op,调用方不崩、零开销。
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from threading import Lock
from typing import Any

# ── 进程内指标快照(不依赖外部系统,/observability 与测试可用) ──
_snapshot_lock = Lock()
_snapshot: dict[str, float] = {}

# ── sdk 注入的 provider 单例 ──
_providers: dict[str, Any] = {}
_counters: dict[str, Any] = {}
_histograms: dict[str, Any] = {}


def install(*, tracer_provider=None, meter=None, root_logger=None) -> None:
    """由 core.observability.sdk.init 在启动时注入。"""
    _providers.clear()
    _counters.clear()
    _histograms.clear()
    if tracer_provider is not None:
        _providers["tracer"] = tracer_provider.get_tracer("codeforge")
    if meter is not None:
        _providers["meter"] = meter
    if root_logger is not None:
        _providers["logger"] = root_logger


def record_metric(name: str, value: float = 1.0, *, delta: bool = True) -> None:
    """记一个进程内指标(计数器累加 or 设置值);尽力写 OTel meter。"""
    with _snapshot_lock:
        if delta:
            _snapshot[name] = _snapshot.get(name, 0.0) + value
        else:
            _snapshot[name] = value
    meter = _providers.get("meter")
    if meter is None:
        return
    # 复用同名 counter,避免重复创建 instrument
    counter = _counters.get(name)
    if counter is None:
        try:
            counter = meter.create_counter(name, description="codeforge metric")
            _counters[name] = counter
        except Exception:  # noqa: BLE001
            return
    try:
        counter.add(value)
    except Exception:  # noqa: BLE001 —— 可观测性失败静默
        pass


def record_histogram(name: str, value: float, *, unit: str = "1") -> None:
    """记一个分布(histogram)到 OTel meter。

    只写 OTel meter 的直方图 instrument,不更新 _snapshot(那是标量计数器,
    与分布不兼容)。可观测性未启用时 get_meter() 返回 _NullMeter,
    create_histogram/.record 均为 no-op,零开销、绝不抛。
    """
    meter = _providers.get("meter")
    if meter is None:
        return
    h = _histograms.get(name)
    if h is None:
        try:
            h = meter.create_histogram(name, description="codeforge histogram", unit=unit)
            _histograms[name] = h
        except Exception:  # noqa: BLE001
            return
    try:
        h.record(value)
    except Exception:  # noqa: BLE001 —— 可观测性失败静默
        pass


def snapshot() -> dict[str, float]:
    with _snapshot_lock:
        return dict(_snapshot)


# ── no-op 桩 ──

class _NullMeter:
    def __getattr__(self, name: str):
        def _noop(*a, **k):
            return self
        return _noop


class _NoopRootLogger:
    """未启用时的根 logger stub:让 `getLogger().debug/info/warning` 走 stdlib 默认。"""

    def __getattr__(self, name: str):
        # 委托给 stdlib 根 logger,避免完全吞掉
        return getattr(logging.getLogger(), name)


# ── 门面 ──


def get_tracer(instrumentation: str = "codeforge"):
    t = _providers.get("tracer")
    if t is None:
        return nullcontext()
    try:
        return t
    except Exception:  # noqa: BLE001
        return nullcontext()


def get_meter(name: str = "codeforge") -> Any:
    m = _providers.get("meter")
    return m if m is not None else _NullMeter()


def get_root_logger() -> Any:
    lg = _providers.get("logger")
    if lg is not None:
        return lg
    return logging.getLogger()
