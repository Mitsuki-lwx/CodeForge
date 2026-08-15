"""团队 feature flag —— FORK_TEAMMATE。

Fork 路径（继承 Lead 完整对话历史的队员）默认关闭，受配置 `features.fork_teammate` 控制。
"""

from __future__ import annotations

from typing import Any


def fork_teammate_enabled(cfg: Any) -> bool:
    """读 cfg.features.fork_teammate；缺字段返回 False。"""
    features = getattr(cfg, "features", None)
    return bool(getattr(features, "fork_teammate", False))


__all__ = ["fork_teammate_enabled"]
