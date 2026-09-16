"""恢复语义：判定「哪些副作用可能已经生效」，并坚持不自动重跑。

崩溃之后，末尾的工具调用点分三类，必须分清——否则恢复会重复执行副作用：

    A. 工具还没执行就崩了
       对话里有未配对的 `tool_use`，journal 里**没有** → 重跑安全
    B. 工具执行完了、结果没落盘
       对话里有未配对的 `tool_use`，journal 里**有** → 不能自动重跑，交人确认
    C. 工具执行完、结果也落盘了
       对话里配对完整 → 无事发生

`find_pending_confirmations()` 就是把 B 挑出来。它**只报告、不执行**：重跑与否是人的
决定——「写文件」重放一次未必等价（追加语义、并发写、外部副作用都可能是非幂等的），
自动重跑等于替用户赌一把。

已知边界：只与**未配对**的 `tool_use` 交叉。journal 里若存在对话中完全找不到的
`tool_use_id`（例如它发生在某次压缩之前，那段历史已被 `compact` 标记整体替换），
不会被报出来——那是压缩的正常结果，不是异常。代价是这类条目需要人自己从
journal 里翻，换来的是不产生假警报（假警报会让人很快学会忽略真警报）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from conversation.message import Message
from core.host.journal import read_journal


@dataclass(frozen=True)
class PendingConfirmation:
    """一个「可能已经生效、但结果没落盘」的工具调用。"""

    tool_use_id: str
    tool_name: str
    summary: str
    ts: int


def find_pending_confirmations(
    session_dir: str | Path,
    unpaired_tool_uses: Sequence[Message],
) -> list[PendingConfirmation]:
    """交叉 journal 与未配对 `tool_use`，挑出可能已生效的副作用调用。

    Args:
        session_dir: 会话目录（含 `journal.jsonl`）。
        unpaired_tool_uses: `RestoreResult.unpaired_tool_uses`。

    Returns:
        按 journal 写入顺序排列的待确认项。空列表表示没有需要人确认的副作用——
        可以放心从截断点继续。
    """
    ids = {m.tool_use_id for m in unpaired_tool_uses if m.tool_use_id}
    if not ids:
        return []
    return [
        PendingConfirmation(
            tool_use_id=e.tool_use_id,
            tool_name=e.tool_name,
            summary=e.summary,
            ts=e.ts,
        )
        for e in read_journal(session_dir)
        if e.tool_use_id in ids
    ]
