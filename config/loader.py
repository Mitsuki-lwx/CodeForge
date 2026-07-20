"""YAML 配置加载与校验。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import yaml

from config.model import ProviderConfig

VALID_PROTOCOLS = {"anthropic", "openai"}


def _validate_providers(providers: list[dict]) -> list[ProviderConfig]:
    """校验原始字典列表并转为 ProviderConfig 列表。"""
    if not providers:
        print("错误：配置中未定义任何 provider。", file=sys.stderr)
        sys.exit(1)

    result: list[ProviderConfig] = []
    for i, raw in enumerate(providers):
        errors: list[str] = []

        name = raw.get("name", "")
        if not name:
            errors.append(f"providers[{i}]：'name' 缺失或为空")

        protocol = raw.get("protocol", "")
        if not protocol:
            errors.append(f"providers[{i}]：'protocol' 缺失或为空")
        elif protocol not in VALID_PROTOCOLS:
            errors.append(
                f"providers[{i}]：'protocol' 必须是 {VALID_PROTOCOLS} 之一，"
                f"实际为 '{protocol}'"
            )

        model = raw.get("model", "")
        if not model:
            errors.append(f"providers[{i}]：'model' 缺失或为空")

        api_key = raw.get("api_key", "")
        if not api_key:
            errors.append(f"providers[{i}]：'api_key' 缺失或为空")

        if errors:
            for err in errors:
                print(f"配置错误：{err}", file=sys.stderr)
            sys.exit(1)

        result.append(
            ProviderConfig(
                name=name,
                protocol=protocol,
                model=model,
                api_key=api_key,
                base_url=raw.get("base_url") or None,
                thinking=bool(raw.get("thinking", False)),
                context_window=int(raw.get("context_window", 0)),
            )
        )

    return result


def load_config(path: str | Path = "config.yaml") -> list[ProviderConfig]:
    """加载并校验 YAML 配置文件。

    返回 ProviderConfig 列表，校验失败时打印错误并退出。
    """
    config_path = Path(path)

    if not config_path.exists():
        print(f"错误：配置文件 {config_path} 不存在。", file=sys.stderr)
        sys.exit(1)

    try:
        raw = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        print(f"错误：配置文件 YAML 格式无效：{e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print("错误：配置文件顶层必须是一个字典。", file=sys.stderr)
        sys.exit(1)

    providers_raw = data.get("providers", [])
    if not isinstance(providers_raw, list):
        print("错误：配置中 'providers' 必须是一个列表。", file=sys.stderr)
        sys.exit(1)

    return _validate_providers(providers_raw)
