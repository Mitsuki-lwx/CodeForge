"""会话宿主（Session Host）子包。

把「会话执行」从 TUI 进程里解耦出来：run 状态持久化、生命周期状态机、副作用
journal、单写者锁、恢复语义，以及本地控制通道。

分层（自下而上，下层不依赖上层）：

- `run_store` —— 状态持久化（SQLite），无业务判断
- `lifecycle` —— 状态迁移规则，含崩溃后的 stale 改判
- `journal`   —— 副作用追加日志，用于恢复时判定哪些调用可能已生效
- `recovery`  —— journal × conversation 交叉成「待确认」列表
- `lock`      —— `session_dir` 级单写者独占锁
- `server` / `client` / `protocol` —— 本地控制通道

`__init__` 只导出已落地且被外部（非 host 包内）引用的符号；各模块按
`docs/tasks_session_host.md` 的顺序逐个加入，不做前瞻性 re-export。

**`server` 刻意不在这里 re-export**：`server.py` 依赖 `core.agent.bootstrap`，而
`bootstrap` 又依赖本包的 `journal` / `lock`。若在本文件里 import `server`，就会出现
「bootstrap → core.host.__init__ → server → bootstrap（半初始化）」的循环导入。调用
方直接 `from core.host.server import start_host`，那时 `bootstrap` 尚未开始加载，
不会有半初始化状态。

`client` **可以**在这里 re-export，这个不对称是查过的、不是疏忽：`client.py` 只依赖
`proc` / `protocol`，不碰 `bootstrap`——它不该为了连一次 host 就把整个 agent 装配栈
拉起来（见 `client.py` 模块 docstring）。所以从 `core.host` 直接拿 `HostClient`
是安全的。
"""

from __future__ import annotations

from core.host.client import HostClient, HostError, HostNotRunningError
from core.host.journal import (
    JOURNAL_FILENAME,
    SIDE_EFFECT_CATEGORIES,
    JournalEntry,
    SideEffectJournal,
    journal_path,
    read_journal,
)
from core.host.lifecycle import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    RunLifecycle,
)
from core.host.lock import LOCK_FILENAME, SessionLock, SessionLockedError
from core.host.proc import pid_alive
from core.host.protocol import (
    DEFAULT_PORT,
    HOST_BIND_ADDRESS,
    HOST_INFO_FILENAME,
    HOST_TOKEN_FILENAME,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    HostInfo,
    ProtocolError,
    clear_host_info,
    decode_frame,
    encode_frame,
    event_frame,
    host_info_path,
    issue_token,
    load_host_info,
    load_token,
    to_jsonable,
    token_matches,
    token_path,
    write_host_info,
)
from core.host.recovery import PendingConfirmation, find_pending_confirmations
from core.host.run_store import (
    ACTIVE_STATUSES,
    RUNS_DB_FILENAME,
    TERMINAL_STATUSES,
    RunNotFoundError,
    RunRecord,
    RunStatus,
    RunStore,
    new_run_id,
)

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_PORT",
    "HOST_BIND_ADDRESS",
    "HOST_INFO_FILENAME",
    "HOST_TOKEN_FILENAME",
    "JOURNAL_FILENAME",
    "LOCK_FILENAME",
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "RUNS_DB_FILENAME",
    "SIDE_EFFECT_CATEGORIES",
    "TERMINAL_STATUSES",
    "HostClient",
    "HostError",
    "HostInfo",
    "HostNotRunningError",
    "IllegalTransitionError",
    "JournalEntry",
    "PendingConfirmation",
    "ProtocolError",
    "RunLifecycle",
    "RunNotFoundError",
    "RunRecord",
    "RunStatus",
    "RunStore",
    "SessionLock",
    "SessionLockedError",
    "SideEffectJournal",
    "clear_host_info",
    "decode_frame",
    "encode_frame",
    "event_frame",
    "find_pending_confirmations",
    "host_info_path",
    "issue_token",
    "journal_path",
    "load_host_info",
    "load_token",
    "new_run_id",
    "pid_alive",
    "read_journal",
    "to_jsonable",
    "token_matches",
    "token_path",
    "write_host_info",
]
