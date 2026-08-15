"""文件锁 —— 邮箱与任务存储共用的并发安全锁。

用 `os.open(O_CREAT|O_EXCL|O_WRONLY)` 抢占锁文件：EEXIST 说明被他人持有。
抢锁失败按 5–100ms 随机抖动重试；持锁超过阈值视为 stale（进程崩溃残留），
清掉后重试一次。退出时 `os.unlink` 释放。
"""

from __future__ import annotations

import asyncio
import errno
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

# 抢锁最大重试次数。
LOCK_MAX_RETRIES = 10
# 超过该秒数视为 stale（进程崩溃残留的锁）。
LOCK_STALE_AFTER = 10.0
# 退避抖动范围（秒）。
LOCK_BACKOFF_MIN = 0.005
LOCK_BACKOFF_MAX = 0.1


def _is_stale(lock_path: Path) -> bool:
    try:
        st = lock_path.stat()
        return (time.time() - st.st_mtime) > LOCK_STALE_AFTER
    except OSError:
        return False


@asynccontextmanager
async def acquire(lock_path: str | Path):
    """抢占锁文件，直到成功或重试耗尽。

    返回一个异步上下文管理器；持有锁的大括号内可安全做 read-modify-write。
    重试耗尽时抛 TimeoutError。
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(LOCK_MAX_RETRIES):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            try:
                yield
            finally:
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
            return
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            # 被他人持有：stale 则清掉重试一次，否则抖动退避。
            if _is_stale(lock_path):
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                continue
            await asyncio.sleep(random.uniform(LOCK_BACKOFF_MIN, LOCK_BACKOFF_MAX))

    raise TimeoutError(f"抢锁失败（{LOCK_MAX_RETRIES} 次重试后仍被持有）: {lock_path}")
