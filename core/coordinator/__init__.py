"""Coordinator Mode —— 开关检测 / 工具白名单 / 系统提示词。

独立的 Lead 指挥模式：配置能力开关 + 环境变量 `CODEFORGE_COORDINATOR_MODE` 双锁。
开启后收窄 Lead 工具集（剥夺 write_file/edit_file），只保留调度、读类与 bash。
不可运行时解锁（N8），取消唯一方式是退出重启。
"""

from __future__ import annotations

import os
from typing import Any

# 双锁开关名（能力开关读 cfg.features.coordinator_mode）。
COORDINATOR_FEATURE = "coordinator_mode"
ENV_VAR = "CODEFORGE_COORDINATOR_MODE"

_truthy_values = {"1", "true", "yes"}


def env_truthy(v: str) -> bool:
    """解析环境变量布尔：'1'/'true'/'yes'（大小写不敏感）为真。"""
    return v.strip().lower() in _truthy_values


def is_enabled(cfg: Any | None = None) -> bool:
    """双锁全开才生效：能力开关且 env var 为真。

    cfg 可以是 config 对象（读 cfg.features.coordinator_mode）或 FeaturesConfig 本身
    （读 cfg.coordinator_mode）。cfg 为 None 或缺少能力开关时返回 False。
    """
    if cfg is None:
        return False
    # 兼容 cfg.features.coordinator_mode 或 cfg.coordinator_mode
    raw = getattr(cfg, "features", cfg)
    if not bool(getattr(raw, COORDINATOR_FEATURE, False)):
        return False
    return env_truthy(os.environ.get(ENV_VAR, ""))


COORDINATOR_ALLOWED_TOOLS: list[str] = [
    "Agent",
    "TeamCreate",
    "TeamDelete",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "SendMessage",
    "read_file",
    "glob",
    "grep",
    "bash",
]


def allowed_tools() -> list[str]:
    """返回 Coordinator 白名单（启动时 set_allowed_tools 用）。"""
    return list(COORDINATOR_ALLOWED_TOOLS)


SYSTEM_PROMPT_SUFFIX: str = (
    "Coordinator 模式：你是团队的 Lead，负责派遣队员与决策，代码修改交给队员。\n"
    "纪律：派完队员就停手等汇报——调用 Agent / SendMessage 后禁止立刻 read_file / "
    "glob / grep / bash 自己探索，也禁止用 sleep / TaskList 轮询凑时间；唯一该做的是"
    "发一行总结结束本轮。只有 Research 首次目标定位、Synthesis 读队员产出报告、"
    "Verification 的 git diff / status 收敛时才允许自己用读类工具。"
)


def system_prompt_suffix() -> str:
    """返回 Coordinator 系统提示词追加段。"""
    return SYSTEM_PROMPT_SUFFIX


__all__ = ["allowed_tools", "env_truthy", "is_enabled", "system_prompt_suffix"]
