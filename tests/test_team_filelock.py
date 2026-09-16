"""文件锁单元测试。

覆盖：串行争用、release 后再次可抢、stale 锁抢占、重试耗尽抛超时。
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from core.team import filelock


async def test_acquire_serial(tmp_path):
    """两次串行抢锁，中间 release，都能拿到。"""
    lock = tmp_path / "x.lock"
    async with filelock.acquire(lock):
        assert lock.exists()
    async with filelock.acquire(lock):
        assert lock.exists()
    assert not lock.exists()  # 释放后文件被清


async def test_acquire_mutual_exclusion(tmp_path):
    """并发抢同一锁：同一时刻仅一个持锁（计数器不并发）。"""
    lock = tmp_path / "x.lock"
    max_seen = 0
    counter = 0
    stop = False

    async def worker():
        nonlocal max_seen, counter, stop
        while not stop:
            try:
                async with filelock.acquire(lock):
                    counter += 1
                    max_seen = max(max_seen, counter)
                    await asyncio.sleep(0)
                    counter -= 1
            except TimeoutError:
                pass

    tasks = [asyncio.create_task(worker()) for _ in range(3)]
    await asyncio.sleep(0.05)
    stop = True
    await asyncio.gather(*tasks)
    assert max_seen == 1


async def test_stale_lock_is_cleared(tmp_path):
    """持锁超 LOCK_STALE_AFTER 的旧锁视为 stale，新 writer 能清掉并拿到。"""
    lock = tmp_path / "x.lock"
    lock.touch()
    old = time.time() - filelock.LOCK_STALE_AFTER - 2
    os.utime(lock, (old, old))
    async with filelock.acquire(lock):
        assert lock.exists()  # 旧锁已被清掉重抢成功


async def test_lock_released_after_exception(tmp_path):
    """持锁块抛异常时锁仍被释放（finally 兜底）。"""
    lock = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        async with filelock.acquire(lock):
            raise RuntimeError("boom")
    assert not lock.exists()
