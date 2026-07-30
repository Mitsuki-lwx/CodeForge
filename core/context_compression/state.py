"""状态对象。

包含：
- SessionContext: 会话生命周期信息（session_id、落盘目录）
- ContentReplacementState: 工具结果替换决策账本（幂等冻结）
- CompactCircuitBreaker: 自动摘要连续失败熔断器
- RecoveryState: ReadFile 后文件追踪状态
- FileReadRecord: 单次文件读取记录
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.context_compression.const import MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES

# ── SessionContext ──────────────────────────────────────────────


def _new_session_id() -> str:
    """生成会话 ID：YYYYMMDD-HHMMSS-xxxx。

    - YYYYMMDD-HHMMSS 为进程启动时刻的本地时间
    - xxxx 为 4 字符随机十六进制后缀（防同秒碰撞）

    secrets.token_hex(2) 失败极少见；降级到 random 兜底。
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 —— 本地时间会话 ID
    try:
        hex_str = secrets.token_hex(2)
    except Exception:  # noqa: BLE001 —— secrets 失败兜底，任何异常都降级 random
        import logging
        import random

        logging.getLogger(__name__).warning("secrets.token_hex 失败，降级到 random")
        hex_str = random.Random(time.time()).randbytes(2).hex()
    return f"{ts}-{hex_str}"


@dataclass
class SessionContext:
    """会话生命周期信息。

    session_id 进程启动时一次性生成（YYYYMMDD-HHMMSS-xxxx）。
    session_dir 指向 .codeforge/sessions/<session_id>/。
    spill_dir 指向 session_dir/tool-results/（工具结果落盘）。
    """

    session_id: str
    spill_dir: str
    session_dir: str = ""


def new_session_context(workspace: str) -> SessionContext:
    """创建 SessionContext 并确保落盘目录存在。

    Args:
        workspace: 项目根目录路径。

    Returns:
        构造好的 SessionContext 实例。
    """
    sid = _new_session_id()
    session_dir = str(Path(workspace) / ".codeforge" / "sessions" / sid)
    spill_dir = str(Path(session_dir) / "tool-results")
    Path(spill_dir).mkdir(parents=True, exist_ok=True)
    return SessionContext(session_id=sid, spill_dir=spill_dir, session_dir=session_dir)


# ── ContentReplacementState ─────────────────────────────────────


class ContentReplacementState:
    """会话级工具结果替换决策账本。

    _seen_ids 记录已决策过的 tool_use_id（无论 kept 还是 replaced）。
    _replacements 只保存"决定替换"的预览字符串。

    并发安全：Python asyncio 单线程事件循环保证串行，
    offload_and_snip 在单次迭代中执行"读账本→决策→落盘→写账本"，
    不跨 async 边界，无需显式锁。
    """

    def __init__(self) -> None:
        self._seen_ids: set[str] = set()
        self._replacements: dict[str, str] = {}

    def decide_once(
        self,
        tool_use_id: str,
        original: str,
        decide: Callable[[], tuple[str, str]],
    ) -> str:
        """原子完成"查账本→决策→写账本"。

        若 id 已 Seen：
          - kept → 返回原 content
          - replaced → 返回 _replacements[id]（复用，不重新调 decide）
        若 id 未 Seen：
          - 调 decide() 回调拿到 (decision, preview)
          - "kept": 写 _seen_ids，不写 _replacements；返回 original
          - "replaced": 写 _seen_ids + _replacements；返回 preview
          - "skip": 不写任何账本；返回 original（下一轮重试）

        Args:
            tool_use_id: 工具调用 ID。
            original: 原始工具结果内容。
            decide: 决策回调，返回 (decision, preview)。
                    decision 为 "kept" | "replaced" | "skip"。

        Returns:
            应使用的 content 字符串（原文或预览体）。
        """
        # 查账本：已 Seen 直接返回存量结果
        if tool_use_id in self._seen_ids:
            return self._replacements.get(tool_use_id, original)

        # 未 Seen → 调回调决策
        decision, preview = decide()

        if decision == "kept":
            self._seen_ids.add(tool_use_id)
            return original
        elif decision == "replaced":
            self._seen_ids.add(tool_use_id)
            self._replacements[tool_use_id] = preview
            return preview
        else:  # "skip"
            return original


# ── CompactCircuitBreaker ───────────────────────────────────────


class CompactCircuitBreaker:
    """自动摘要连续失败熔断器。

    跟踪自动摘要连续失败次数，达到阈值后跳闸。
    手动 / 紧急压缩路径不读此字段。

    并发安全：Python asyncio 单线程事件循环保证串行。
    """

    def __init__(self) -> None:
        self._consecutive_failures = 0

    def record_success(self) -> None:
        """记录一次成功，清零连续失败计数。"""
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """记录一次失败，连续失败计数 +1。"""
        self._consecutive_failures += 1

    def tripped(self) -> bool:
        """是否已跳闸（连续失败数 ≥ 阈值）。"""
        return self._consecutive_failures >= MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES


# ── FileReadRecord / RecoveryState ──────────────────────────────


@dataclass
class FileReadRecord:
    """单次文件读取记录。

    content 是不带行号前缀的纯净字节（由 Agent 在 ReadFile 后重读磁盘获取）。
    """

    path: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)


class RecoveryState:
    """Agent 主循环写入、compact 摘要时读取的文件追踪状态。

    _files 键为文件绝对路径，避免相对路径在不同 cwd 下错乱。

    并发安全：Python asyncio 单线程事件循环保证串行。
    """

    def __init__(self) -> None:
        self._files: dict[str, FileReadRecord] = {}

    def record_file(self, path: str, content: str) -> None:
        """记录一次文件读取。

        若 path 不是绝对路径则 resolve 一次再存。
        写入时 timestamp = datetime.now()。

        Args:
            path: 文件路径。
            content: 不带行号前缀的纯净文件内容。
        """
        abs_path = str(Path(path).resolve())
        self._files[abs_path] = FileReadRecord(
            path=abs_path,
            content=content,
            timestamp=datetime.now(),  # noqa: DTZ005 —— 内部排序用，naive 时间一致可比
        )

    def snapshot(self) -> list[FileReadRecord]:
        """返回按 timestamp 倒序排序的拷贝列表。

        返回值是浅拷贝，FileReadRecord 字段不可变所以足够安全。
        """
        records = list(self._files.values())
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records
