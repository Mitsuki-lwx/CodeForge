"""进程探活测试。

`pid_alive` 是 stale 改判与单写者锁的共同地基，且两个方向都会出问题：判死了会把
正在跑的 run 标成 interrupted（状态损坏），判活着会让崩溃的 run 永远留在列表里。
所以存活与已死两个分支都要真跑一遍——尤其 Windows 分支（ctypes 调 kernel32）
只有在 Windows 上才被执行，纯逻辑测试覆盖不到。
"""

from __future__ import annotations

import os
import subprocess
import sys

from core.host.proc import pid_alive


def test_current_process_is_alive():
    assert pid_alive(os.getpid())


def test_running_child_is_alive():
    """「存活」分支必须真的能返回 True。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid)
    finally:
        proc.kill()
        proc.wait()


def test_exited_process_is_not_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not pid_alive(proc.pid)


def test_none_and_nonpositive_pids_are_not_alive():
    assert not pid_alive(None)
    assert not pid_alive(0)
    assert not pid_alive(-1)
