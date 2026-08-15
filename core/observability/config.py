"""可观测性配置。

读环境变量(OTEL_*)与 config.yaml 可选 `observability:` 段,决定：
  - 是否启用(默认启用,本地 JSONL/Console)
  - 外部导出:OTEL_OTLP_ENDPOINT 或 observability.endpoint 配置后走 OTLP
  - 本地落盘根目录(默认 ~/.codeforge/obs)
  - 采样率(默认 1.0)

尽可能全走 env(OTEL 惯例),config.yaml 的 observability 段作为补充。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ObservabilityConfig:
    enabled: bool = True
    local_dir: Path = Path.home() / ".codeforge" / "obs"
    otlp_endpoint: str | None = None
    otlp_headers: dict[str, str] | None = None
    sample_rate: float = 1.0
    # 日志级别覆盖(默认 WARNING 兜底,可调到 INFO)
    log_level: str = "WARNING"


def load_observability_config() -> ObservabilityConfig:
    """从 env + config.yaml 读取配置。env 优先,未显式关闭即为本地模式。"""
    cfg = ObservabilityConfig()

    if os.getenv("CODEFORGE_OBSERVABILITY", "1").lower() in ("0", "false", "off", "no"):
        cfg.enabled = False
        return cfg

    # env: OTEL 惯例
    ep = os.getenv("OTEL_OTLP_ENDPOINT")
    if ep:
        cfg.otlp_endpoint = ep
    headers_raw = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    if headers_raw:
        cfg.otlp_headers = {
            part.split("=", 1)[0]: part.split("=", 1)[1]
            for part in headers_raw.split(",")
            if "=" in part
        }
    rate = os.getenv("OTEL_TRACES_SAMPLER_ARG") or os.getenv("CODEFORGE_SAMPLE_RATE")
    if rate:
        try:
            cfg.sample_rate = float(rate)
        except ValueError:
            pass
    level = os.getenv("CODEFORGE_LOGGING_LEVEL")
    if level:
        cfg.log_level = level.upper()

    root = os.getenv("CODEFORGE_OBS_DIR")
    if root:
        cfg.local_dir = Path(root)

    # config.yaml 补充(可选 observability 段)
    _merge_yaml(cfg)
    return cfg


def _merge_yaml(cfg: ObservabilityConfig) -> None:
    """从 config.yaml 的 observability 段补充(env 未覆盖的字段)。"""
    try:
        import yaml

        path = Path(os.getenv("CODEFORGE_CONFIG", "config.yaml"))
        if not path.exists():
            return
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        obs = data.get("observability")
        if not isinstance(obs, dict):
            return
        if cfg.otlp_endpoint is None and obs.get("endpoint"):
            cfg.otlp_endpoint = str(obs["endpoint"])
        # 嵌套 headers(如 Authorization: Basic <base64>),env 未设时才采用
        if cfg.otlp_headers is None and isinstance(obs.get("headers"), dict):
            cfg.otlp_headers = {str(k): str(v) for k, v in obs["headers"].items()}
        if obs.get("local_dir") and not os.getenv("CODEFORGE_OBS_DIR"):
            cfg.local_dir = Path(obs["local_dir"])
        if obs.get("log_level") and not os.getenv("CODEFORGE_LOGGING_LEVEL"):
            cfg.log_level = str(obs["log_level"]).upper()
    except Exception:  # noqa: BLE001 —— 配置读取失败不阻断
        return
