"""YAML 配置加载与校验。"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from config.model import ProviderConfig

VALID_PROTOCOLS = {"anthropic", "openai"}
# 已知上游厂商（vendor）。未知 vendor 仅告警回退自动识别，不阻断启动。
KNOWN_VENDORS = {"anthropic", "openai", "deepseek"}


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

        vendor = raw.get("vendor") or None
        if vendor and vendor not in KNOWN_VENDORS:
            print(
                f"警告：providers[{i}]：未知 vendor '{vendor}'，将按自动识别处理。",
                file=sys.stderr,
            )
            vendor = None

        result.append(
            ProviderConfig(
                name=name,
                protocol=protocol,
                model=model,
                api_key=api_key,
                base_url=raw.get("base_url") or None,
                thinking=bool(raw.get("thinking", False)),
                context_window=int(raw.get("context_window", 0)),
                vendor=vendor,
                tier=str(raw.get("tier", "") or ""),
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


def load_config_full(path: str | Path = "config.yaml") -> tuple[list[ProviderConfig], object]:
    """加载配置并返回 (providers, features)。

    与 load_config 兼容（保持既有调用 `load_config` 返回 providers 列表不变），
    额外解析 `features:` 段为 FeaturesConfig（团队/coordinator 开关）。
    """
    config_path = Path(path)
    if not config_path.exists():
        return load_config(path), _default_features()


    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return load_config(path), _default_features()
    if not isinstance(data, dict):
        return load_config(path), _default_features()

    providers = _validate_providers(data.get("providers", []))
    raw_features = data.get("features", {})
    features = _default_features()
    features.loop = str(data.get("loop", "") or "")  # 顶层 loop:（spec_loop）
    if isinstance(raw_features, dict):
        features.coordinator_mode = bool(raw_features.get("coordinator_mode", False))
        features.fork_teammate = bool(raw_features.get("fork_teammate", False))
        raw_router = raw_features.get("router", {})
        if isinstance(raw_router, dict):
            from config.model import RouterConfig

            features.router = RouterConfig(
                enabled=bool(raw_router.get("enabled", False)),
                judge_prompt=str(raw_router.get("judge_prompt", "") or ""),
                cheap_tier=str(raw_router.get("cheap_tier", "") or "cheap"),
            )
        raw_host = raw_features.get("host", None)
        if isinstance(raw_host, dict):
            features.host = _parse_host_config(raw_host)
    return providers, features


def _parse_host_config(raw: dict) -> object:
    """解析 `features.host`。

    非法值一律**告警并退回默认**，不阻断启动 —— 配置写错的代价不该是"起不来"，
    而 host 的档位本身有安全默认（`deny_all`）。

    合法档位取自 `core.permissions.modes.UnattendedPolicy`（单一事实来源），
    避免这里手抄一份字符串集合、日后与实现漂移。
    """
    from config.model import HostConfig
    from core.permissions.modes import UnattendedPolicy

    valid = sorted(p.value for p in UnattendedPolicy)
    policy = str(raw.get("unattended_policy", "") or "deny_all").strip()
    if policy not in valid:
        print(
            f"警告：features.host.unattended_policy='{policy}' 不是有效档位"
            f"（可选 {valid}），已按 'deny_all' 处理。",
            file=sys.stderr,
        )
        policy = "deny_all"

    try:
        port = int(raw.get("port", 0) or 0)
    except (TypeError, ValueError):
        print("警告：features.host.port 不是整数，已按 0（随机端口）处理。", file=sys.stderr)
        port = 0
    if not 0 <= port <= 65535:
        print(
            f"警告：features.host.port={port} 超出 0-65535，已按 0（随机端口）处理。",
            file=sys.stderr,
        )
        port = 0

    token_file = str(raw.get("token_file", "") or "").strip()
    if token_file:
        # 静默忽略一个**安全相关**配置是最糟的：用户会以为凭据已经挪出项目目录，
        # 实际仍写在 <workspace>/.codeforge/host.token。宁可明确说一句"没生效"。
        print(
            "警告：features.host.token_file 尚未生效，控制通道凭据仍写在 "
            "<workspace>/.codeforge/host.token；该配置当前不产生任何效果"
            "（会合信息 host.json 同样固定在 .codeforge/ 下，只挪凭据会让两者分处两地）。",
            file=sys.stderr,
        )

    return HostConfig(
        enabled=bool(raw.get("enabled", False)),
        port=port,
        token_file=token_file,
        unattended_policy=policy,
    )


def _default_features():
    from config.model import FeaturesConfig

    return FeaturesConfig()


def load_host_config(path: str | Path = "config.yaml"):
    """读 `features.host`，**永不抛异常**：读不出来就返回默认档（`HostConfig()`）。

    默认档即「全关 + `deny_all` + 随机端口」，所以调用方拿到默认值等于「没配 host」，
    不需要再判 `None`。两个调用点（`main.py` 的 host 子命令、`tui/host_mode.py` 的
    内嵌回落）共用这一处，避免两边各写一份默认档位、日后漂移。

    配置读不出来时只告警不阻断：host 起不来通常比「用默认档起来」更糟——这与
    `_parse_host_config` 对非法档位的取舍一致。
    """
    from config.model import HostConfig

    host = None
    try:
        _, features = load_config_full(path)
        host = getattr(features, "host", None)
    except SystemExit:
        # 配置文件本身不合法时 `load_config_full` 会经 `load_config` 直接退出（既有
        # 严格行为，主流程靠它给出明确错误）。本函数的契约只是"回答 host 段"，不该由
        # 它决定进程去死，所以也接住——按默认档（关）处理，真正的错误由主流程报。
        print(
            "警告：配置文件不合法，features.host 按默认档（关）处理。", file=sys.stderr
        )
    except Exception as e:  # noqa: BLE001 —— 配置问题不该让 host 起不来
        print(f"警告：读取 features.host 失败（{e}），按默认档处理。", file=sys.stderr)
    return host if isinstance(host, HostConfig) else HostConfig()
