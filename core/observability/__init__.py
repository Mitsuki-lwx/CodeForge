"""Observability:Logs + Metrics + Traces。

对外公共入口:
  - ensure_initialized():幂等初始化(由 main/tui 启动时调用)
  - shutdown():进程结束清理(可选)
  - get_tracer / get_meter / get_logger:采集门面
  - record_metric / snapshot:进程内指标(本地读取、测试)

未启用或未安装 opentelemetry-sdk 时,所有入口返回 no-op,零开销、绝不阻断主流程。
"""

from __future__ import annotations

from core.observability import sdk
from core.observability.providers import (
    get_meter,
    get_tracer,
    record_metric,
    snapshot,
)

__all__ = [
    "ensure_initialized",
    "get_meter",
    "get_tracer",
    "record_metric",
    "shutdown",
    "snapshot",
]


def ensure_initialized() -> bool:
    return sdk.ensure_initialized()


def shutdown() -> None:
    sdk.shutdown()
