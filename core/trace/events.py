"""Trace 事件 schema。

每类事件都携带一组稳定字段,`to_dict()` 输出键有序(dict 保持插入序),
`ensure_ascii=False` 保证中文不被转义 —— 与 core/hooks/events.py 的稳定输出约定一致。

通用字段(所有事件):
  event        事件类别,取值见各子类 event 常量
  session_id   会话标识(即存档文件所在会话)
  sequence     会话内单调递增序号(由 TraceWriter 在写路径自增)
  ts           epoch 毫秒(事件产生时刻)
  duration_ms  耗时(毫秒,可空——无计时语义的事件不填)
  token        用量 dict(可空;压缩/结束事件才填)
  parent_span_id  父 span 关联(T6);默认空串 = 顶层事件

工具/权限/hook 专有字段:
  tool_use_id  工具调用 id(工具相关)
  tool_name    工具名
  decision     allow/deny/ask(权限类)
  reason       决策/拦截理由
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class _Base:
    """事件基类:稳定字段 + 有序序列化。"""

    session_id: str = ""
    sequence: int = 0
    ts: int = 0  # epoch 毫秒
    duration_ms: int | None = None
    token: dict | None = None
    parent_span_id: str = ""

    @property
    def event(self) -> str:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        """键有序 dict;空的可空字段(duration_ms/token/parent_span_id)不回写。

        通用字段固定在前(事件→会话→序号→时间→可选),子类专有字段在后。
        """
        data = asdict(self)
        out: dict[str, Any] = {"event": self.event}
        # 通用字段固定顺序;可选字段为空时省略
        for key in ("session_id", "sequence", "ts", "duration_ms", "token", "parent_span_id"):
            val = data.get(key)
            if val is None or val == "":
                continue
            out[key] = val
        # 子类专有字段(不在通用字段内)按 asdict 插入序并入
        for key, val in data.items():
            if key not in out:
                # 可空专有字段(如 result_preview/phase)空值也省略,保持行精简
                if val is None or val == "":
                    continue
                out[key] = val
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=False)


@dataclass(slots=True)
class ToolStartEvent(_Base):
    """工具调用开始。"""

    tool_use_id: str = ""
    tool_name: str = ""

    @property
    def event(self) -> str:
        return "tool_start"


@dataclass(slots=True)
class ToolEndEvent(_Base):
    """工具调用结束:耗时、成功与否、结果预览(截断到 result_preview_max_chars)。"""

    tool_use_id: str = ""
    tool_name: str = ""
    success: bool = True
    result_preview: str = ""

    @property
    def event(self) -> str:
        return "tool_end"


@dataclass(slots=True)
class PermissionEvent(_Base):
    """权限决策:放行/拒绝/询问 + 理由。"""

    tool_use_id: str = ""
    tool_name: str = ""
    decision: str = "allow"  # allow | deny | ask
    reason: str = ""

    @property
    def event(self) -> str:
        return "permission"


@dataclass(slots=True)
class HookEvent(_Base):
    """hook 前置拦截:是否阻止及理由。"""

    tool_name: str = ""
    blocked: bool = False
    reason: str = ""

    @property
    def event(self) -> str:
        return "hook"


@dataclass(slots=True)
class CompactEvent(_Base):
    """上下文压缩生命周期事件:前后 token 用量。"""

    phase: str = "before_auto"  # before_auto/after_auto/before_emergency/after_emergency
    before_tokens: int = 0
    after_tokens: int = 0

    @property
    def event(self) -> str:
        return "compact"


@dataclass(slots=True)
class AgentErrorEvent(_Base):
    """Agent 异常停止。"""

    code: str = "unknown"  # max_iterations/unknown_tool/stream_error/cancelled/ptl_error

    @property
    def event(self) -> str:
        return "agent_error"


@dataclass(slots=True)
class AgentEndEvent(_Base):
    """Agent 正常结束:耗时与总用量。"""

    elapsed_s: float = 0.0

    @property
    def event(self) -> str:
        return "agent_end"


# 事件联合类型
TraceEvent = (
    ToolStartEvent
    | ToolEndEvent
    | PermissionEvent
    | HookEvent
    | CompactEvent
    | AgentErrorEvent
    | AgentEndEvent
)
