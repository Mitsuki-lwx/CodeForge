"""进程存活探测。

`mark_stale_interrupted()`（任务 5）与单写者锁（任务 8）要回答同一个问题：
「记在文件里的那个 pid 还活着吗？」——所以单独放一层，避免两处各写一份平台分支。

**不要用 `os.kill(pid, 0)` 判活。** 这个 POSIX 惯用法在 Windows 上是陷阱：CPython
把 `os.kill` 的任何非 `CTRL_C_EVENT` / `CTRL_BREAK_EVENT` 信号都实现为
`OpenProcess` + `TerminateProcess`，所以传 0 会**真的把目标进程杀掉**（退出码 0）。
拿探活去杀进程，属于那种只在生产环境发生一次的 bug。
"""

from __future__ import annotations

import functools
import os
from typing import Any

# WaitForSingleObject 返回值：0x102 = 超时（句柄仍可等待 → 进程未退出）
_WAIT_TIMEOUT = 0x00000102
# OpenProcess 权限：只需等待权，不要 PROCESS_ALL_ACCESS（权限越小越不容易被拒）
_SYNCHRONIZE = 0x00100000
# OpenProcess 失败错误码：5 = 拒绝访问（进程存在但拿不到句柄）
_ERROR_ACCESS_DENIED = 5


def pid_alive(pid: int | None) -> bool:
    """`pid` 对应的进程是否仍存活。

    `pid` 为 `None` 或非正数视为**不存活**——调用方只有在确实没有进程可归属时才会
    这么记录，那它就不是一个正在跑的 run。

    判断不清时一律按「存活」处理（未知错误码、权限不足等）。方向性取舍：误判
    「活着的进程已死」会让恢复逻辑把正在跑的 run 标成 `interrupted`，是状态损坏；
    误判「死掉的进程还活着」只是少发现一次异常，代价小得多。
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # 信号 0 只做存在性/权限检查，不投递
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在，只是不属于当前用户
    except OSError:
        return True  # 未知错误：按存活处理（见 pid_alive 的方向性取舍）
    return True


@functools.cache
def _win_api() -> tuple[Any, Any, Any, Any]:
    """惰性加载 kernel32 入口（只在 Windows 分支调用）。

    返回 `(OpenProcess, WaitForSingleObject, CloseHandle, ctypes)`。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE

    wait_for_single = kernel32.WaitForSingleObject
    wait_for_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single.restype = wintypes.DWORD

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    return open_process, wait_for_single, close_handle, ctypes


def _pid_alive_windows(pid: int) -> bool:
    """Windows：`OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`。

    句柄可等待即进程未退出。比 `GetExitCodeProcess == STILL_ACTIVE(259)` 更准——
    退出码 259 本身是合法的用户退出码，会误判。
    """
    open_process, wait_for_single, close_handle, ctypes = _win_api()
    handle = open_process(_SYNCHRONIZE, False, pid)
    if not handle:
        # 拒绝访问说明进程存在但拿不到句柄（跨用户 / 提权进程）→ 按存活处理
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        return wait_for_single(handle, 0) == _WAIT_TIMEOUT
    finally:
        close_handle(handle)
