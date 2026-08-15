"""OpenTelemetry SDK 组装与初始化。

职责:
  - 读配置(env + config.yaml observability 段)
  - 未启用/未安装 opentelemetry-sdk → 不初始化(保持 no-op)
  - 默认:三个 JSONL sink(logs/metrics/traces)落 `~/.codeforge/obs/{}/data.jsonl`
    + Console 可选;不配端点时零网络出口。
  - 配了 OTEL_OTLP_ENDPOINT → 额外叠加 OTLP exporter(条件 import,
    未安装相关包时仅本地,不崩)。
  - 设置统一 stdlib logging 格式/级别,接入 LoggerProvider(若有)。
  幂等:`ensure_initialized()` 可反复调用。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.observability import providers
from core.observability.config import load_observability_config
from core.observability.exporters import JsonlSink

_init_lock = threading.Lock()
_initialized = False
_sinks: dict[str, JsonlSink] = {}


def _build_resource() -> Any:
    """构造 OTel Resource(带 service.name=codeforge)。"""
    from opentelemetry.sdk.resources import Resource

    return Resource.create({"service.name": "codeforge"})


def _build_trace_exporters(cfg) -> list:
    """默认 JSONL + 可选 OTLP 的 span exporters。"""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class _JsonlSpanExporter(SpanExporter):
        def __init__(self, sink: JsonlSink) -> None:
            self._sink = sink

        def export(self, spans, **kwargs):
            for span in spans:
                self._sink.write_line(_span_to_dict(span))
            return SpanExportResult.SUCCESS

        def shutdown(self, timeout_millis: int = 30000, **kwargs):
            self._sink.close()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    exporters = [_JsonlSpanExporter(_sink("traces"))]
    if cfg.otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporters.append(OTLPSpanExporter(endpoint=cfg.otlp_endpoint, headers=cfg.otlp_headers))
        except Exception:  # noqa: BLE001 —— OTLP 包未装则跳过,仅本地
            pass
    return exporters


def _build_metric_exporter(cfg) -> Any:
    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

    class _JsonlMetricExporter(MetricExporter):
        # 与 ConsoleMetricExporter 一致:None → 由 reader 选择 temporality
        _preferred_temporality = None
        _preferred_aggregation = None

        def __init__(self, sink: JsonlSink) -> None:
            self._sink = sink

        def export(self, metrics_data, timeout_millis: float = 30000, **kwargs):
            # metrics_data 是 MetricsData:resource_metrics[].scope_metrics[].metrics[]
            for rm in metrics_data.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        self._sink.write_line(_metric_to_dict(m))
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis: float = 30000) -> bool:
            return True

        def shutdown(self, timeout_millis: float = 30000, **kwargs):
            self._sink.close()

    return _JsonlMetricExporter(_sink("metrics"))


def ensure_initialized() -> bool:
    """幂等初始化;返回是否真正启用了 OTel。"""
    global _initialized
    if _initialized:
        return True
    with _init_lock:
        if _initialized:
            return True
        cfg = load_observability_config()
        if not cfg.enabled:
            return False
        try:
            import opentelemetry.sdk  # noqa: F401
        except Exception:  # noqa: BLE001 —— 未安装 sdk,降级 no-op
            return False

        _init(cfg)
        _initialized = True
        return True


def _sink(subdir: str) -> JsonlSink:
    if subdir not in _sinks:
        _sinks[subdir] = JsonlSink(cfg_dir(subdir))
    return _sinks[subdir]


def cfg_dir(subdir: str):
    import os
    from pathlib import Path

    base = os.getenv("CODEFORGE_OBS_DIR") or str(Path.home() / ".codeforge" / "obs")
    return Path(base) / subdir


def _init(cfg) -> None:
    global _initialized
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    # ── Traces ──
    provider = TracerProvider(resource=_build_resource())
    for exp in _build_trace_exporters(cfg):
        provider.add_span_processor(SimpleSpanProcessor(exp))
    _tracer_provider = provider
    try:
        from opentelemetry.semconv.trace import SpanAttributes  # noqa: F401
    except Exception:  # noqa: BLE001
        pass

    # ── Metrics ──
    metric_exporters = []
    metric_exporters.append(PeriodicExportingMetricReader(_build_metric_exporter(cfg), export_interval_millis=5_000))
    # Console 仅当显式开启(保留零打扰默认)
    if _debug_console():
        metric_exporters.append(ConsoleMetricExporter())
    meter_provider = MeterProvider(
        resource=_build_resource(),
        metric_readers=metric_exporters,
    )
    meter = meter_provider.get_meter("codeforge")

    # ── Logging ──
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.WARNING),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    root_logger = logging.getLogger()
    _setup_otel_logging(root_logger, cfg)

    providers.install(
        tracer_provider=provider,
        meter=meter,
        root_logger=root_logger,
    )
    _initialized = True


def _debug_console() -> bool:
    import os

    return os.getenv("CODEFORGE_OBS_CONSOLE", "0").lower() in ("1", "true", "yes")


def _setup_otel_logging(root_logger, cfg) -> None:
    # 用 stdlib handler 把日志也写到本地 JSONL(不依赖 OTel log sdk,降低复杂度)
    handler = _JsonlLogHandler(_sink("logs"))
    root_logger.addHandler(handler)


class _JsonlLogHandler(logging.Handler):
    """把 stdlib 日志行写成 JSONL。"""

    def __init__(self, sink: JsonlSink) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.write_line({
            "ts": int(record.created * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": self.format(record),
        })


def _span_to_dict(span):
    return {
        "trace_id": span.context.trace_id if span.context else None,
        "span_id": span.context.span_id if span.context else None,
        "name": span.name,
        "parent_span_id": span.parent.span_id if span.parent else None,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "attributes": dict(span.attributes or {}),
        "status": span.status.status_code.value,
    }


def _metric_to_dict(m) -> dict:
    """把一条 Metric 写成 dict(名称/单位/计数样本)。"""
    samples = []
    try:
        for r in m.data.data_points:
            if hasattr(r, "value"):
                val = getattr(r, "value", None)
            else:
                # 直方图数据点没有 .value,取分布字段
                val = {
                    "count": getattr(r, "count", None),
                    "sum": getattr(r, "sum", None),
                    "min": getattr(r, "min", None),
                    "max": getattr(r, "max", None),
                }
            samples.append({
                "attributes": dict(r.attributes or {}),
                "value": val,
                "start_time_unix_nano": getattr(r, "start_time_unix_nano", None),
                "time_unix_nano": getattr(r, "time_unix_nano", None),
            })
    except Exception:  # noqa: BLE001
        samples = []
    return {
        "name": m.name,
        "description": getattr(m, "description", ""),
        "unit": getattr(m, "unit", ""),
        "datapoints": samples,
    }


def remove_handler(root_logger, handler):
    root_logger.removeHandler(handler)


def shutdown() -> None:
    global _initialized
    for s in _sinks.values():
        s.close()
    _sinks.clear()
    _initialized = False
