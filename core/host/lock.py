"""`session_dir` 级单写者独占锁。

**要解决的问题**：同一个会话目录被两个进程同时写。`conversation.jsonl` 是只追加的，
两个写者交错追加不会"损坏"文件，但会让对话历史出现**两个进程各自的半截上下文**——
恢复时读到的是两段互不衔接的对话，而没有任何地方报错。这是那种不报错、只是慢慢变
成垃圾的故障，比崩溃难查得多。

**机制**：锁 = 锁文件存在，文件内容 = 持有者 pid。

- 抢占用 `os.open(O_CREAT|O_EXCL|O_WRONLY)`——这一个调用是原子的，没有 TOCTOU 窗口。
- 判"还有人在用吗"靠 `pid_alive()`（`core/host/proc.py`），**不靠时间**。基于时间的
  stale 判定（`core/team/filelock.py` 的 10 秒阈值）对短临界区够用，但会话可以连续跑
  几个小时，时间阈值在这里只会误伤长任务。
- 持有者已死 → 锁文件是崩溃残留，清掉重抢。

**写入后回读校验**：`os.open` 成功到 `os.write` 完成之间有个极窄的窗口。若另一个进程
恰好在此刻把我们的锁文件删掉并建了自己的，我们的 `os.write` 会写进一个**已被 unlink
的孤儿 inode**——静默无效。所以写完必须回读路径确认里面是我们的 pid，否则认输。
不做这一步，理论上存在"两个进程都以为自己持锁"的窗口。

**Windows 兼容**：全程只用 `os.open` / `os.write` / `Path.unlink`，不碰 `fcntl`，
不用 `AF_UNIX`，不用 named pipe。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from core.host.proc import pid_alive

logger = logging.getLogger(__name__)

# 锁文件名（位于 session_dir 下）
LOCK_FILENAME = "writer.lock"

# 抢锁尝试次数。正常情况下第一次就成功；只有「清掉 stale 锁」或「和别人抢同一个
# stale 锁」才需要重试，故次数很少即可。
LOCK_MAX_ATTEMPTS = 5

# 重试间隔（秒）。只为了让"两个进程同时清 stale 锁"有先后，不需要随机抖动。
LOCK_RETRY_DELAY = 0.05


class SessionLockedError(RuntimeError):
    """会话已被其他进程占用。"""

    def __init__(self, session_dir: str | Path, holder_pid: int | None) -> None:
        self.session_dir = str(session_dir)
        self.holder_pid = holder_pid
        who = f"进程 {holder_pid}" if holder_pid is not None else "另一个进程"
        super().__init__(f"该会话已被{who}占用，不能同时写入: {self.session_dir}")


class SessionLock:
    """`<session_dir>/writer.lock` 独占锁。

    `pid` 默认取当前进程。同一实例重复 `acquire()` 是幂等的（直接返回），便于
    "谁拿到谁负责释放"的写法不必层层传状态。

    用法：

        lock = SessionLock(session_dir)
        lock.acquire()          # 失败抛 SessionLockedError
        try:
            ...
        finally:
            lock.release()

    或 `with SessionLock(session_dir):`。
    """

    def __init__(self, session_dir: str | Path, *, pid: int | None = None) -> None:
        self._dir = Path(session_dir)
        self._path = self._dir / LOCK_FILENAME
        self._pid = os.getpid() if pid is None else pid
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def pid(self) -> int:
        """本锁代表的进程 pid（默认即当前进程）。"""
        return self._pid

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> None:
        """抢锁；被占用时抛 `SessionLockedError`。

        幂等：已持有则直接返回。
        """
        if self._held:
            return
        self._dir.mkdir(parents=True, exist_ok=True)

        for attempt in range(LOCK_MAX_ATTEMPTS):
            if self._try_create():
                self._held = True
                return
            holder = self.read_holder()
            if holder is not None and pid_alive(holder):
                raise SessionLockedError(self._dir, holder)
            # 持有者已死，或锁文件没留下可解析的 pid（只可能来自「创建后立刻崩溃」
            # ——那个窗口里持有者不可能还活着）→ 视为崩溃残留，清掉重抢。
            logger.debug(
                "清理 stale 锁（持有者 pid=%s，第 %d 次尝试）: %s",
                holder,
                attempt + 1,
                self._path,
            )
            self._unlink_quietly()
            time.sleep(LOCK_RETRY_DELAY)

        raise SessionLockedError(self._dir, self.read_holder())

    def release(self) -> None:
        """释放锁（幂等）。未持有时什么也不做。

        只删自己的锁文件由 `_held` 保证：没抢到锁的实例调用 `release()` 不会把
        别人的锁删掉。
        """
        if not self._held:
            return
        self._held = False
        self._unlink_quietly()

    def read_holder(self) -> int | None:
        """读锁文件里的持有者 pid；文件不存在或内容不可解析时返回 `None`。"""
        try:
            text = self._path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        try:
            pid = int(text)
        except ValueError:
            return None
        return pid if pid > 0 else None

    # ── 内部 ────────────────────────────────────────────────────

    def _try_create(self) -> bool:
        """原子创建锁文件并写入自己的 pid；返回是否真正持锁。"""
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            os.write(fd, str(self._pid).encode("ascii"))
        finally:
            os.close(fd)
        # 回读校验：写进孤儿 inode 是静默失败的，路径上的内容才是唯一可靠判据
        return self.read_holder() == self._pid

    def _unlink_quietly(self) -> None:
        """尽力删除锁文件；删不掉只告警，**绝不抛出**。

        只吞 `FileNotFoundError` 是不够的：Windows 上文件被别的进程占用会抛
        `PermissionError`，而锁文件恰恰是多进程都会碰的那个。两个调用点都经不起抛：
        - `release()` 在**关闭路径**上（`Bundle.close() → Writer.close() → release()`）。
          删不掉锁的后果只是残留一个锁文件（还有 stale 回收按 pid 判活兜底），
          而抛出去会让整个 teardown 中断——后续清理不执行、run 落不到终态。
          （2026-09-19 实测踩到：`wait_closed()` 卡在这里，run 停在 running。）
        - `acquire()` 的 stale 回收路径靠循环重试，抛出会跳过退避重试。
        失败必须留痕：完全静默会让"锁为什么没清掉"变成下一个无从下手的谜。
        """
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(
                "无法删除锁文件 %s（本次释放继续；stale 回收会按 pid 判活处理）：%s",
                self._path,
                e,
            )

    def __enter__(self) -> SessionLock:  # noqa: PYI034 —— 返回 self，与 Writer 同构
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
