"""后台 shell 进程注册表 —— `bash(run_in_background=true)` 的落点。

**为什么需要它**：`bash` 是同步的（跑完才返回，默认 120s 上限）。要验证一个
web 应用，agent 必须"先把 dev server 起起来，再在**后续的工具调用里**访问它"
—— 同步 bash 做不到这件事（等 server 退出就永远等不到）。Claude Code 用
`BashOutput` / `KillShell` 两个工具解决，这里同构。

设计要点：

* **进程属于一次会话**：注册表挂在 `ExecutionContext.shells` 上（`default_factory`
  每个会话一份），随会话结束一起清掉。子 Agent 有自己的 context，看不到父的
  后台 shell —— 这是刻意的隔离，不是遗漏。
* **输出有上限**：后台进程可能刷日志刷到内存爆掉。缓冲按 `MAX_BUFFER_CHARS`
  截断并**保留尾部**（看错误一般看尾），`truncated` 标记说明发生过截断。
* **不重试**：`bash` / `kill_shell` 的 `max_retries = 0`（见各自类）。注册表这一层
  也不再做任何"失败就重来"—— 重跑一条已经跑了一半的命令是危险的。
* **进程退出兜底**：模块级 `atexit` 把还活着的后台进程杀掉，避免"会话没了、
  dev server 还在占端口"。

线程模型：所有操作都在同一个事件循环里（工具都是 async），所以不加锁。
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 单个后台 shell 的输出缓冲上限（字符）。超过则丢**最早**的块，保留尾部。
MAX_BUFFER_CHARS = 200_000

#: `bash_output` 单次等待的上限（毫秒）。超过这个就别等了，让 agent 下一轮再查
#: —— 一次工具调用挂太久会把整个回合堵住。
MAX_WAIT_MS = 30_000

#: 还活着的后台进程（跨注册表）。仅供 `atexit` 兜底清理。
_LIVE_PROCS: set[asyncio.subprocess.Process] = set()


def _kill_quietly(proc: asyncio.subprocess.Process) -> None:
    """杀**整棵进程树**，不只是 shell 包装进程。

    ⚠️ 这是实测踩出来的：`create_subprocess_shell` 在 Windows 上起的是
    `cmd.exe /c ...`，`proc.kill()` 只杀 cmd，**真正干活的子进程会变成孤儿继续跑**
    —— 表现是"kill_shell 说停掉了，端口还在监听"。POSIX 上同理：`sh -c` 未必
    exec 掉自己，子进程会留在同一个进程组里。

    所以：
    * Windows → `taskkill /F /T /PID`（`/T` = 连子孙一起）；
    * POSIX → 子进程用 `start_new_session=True` 独立成组，这里按**进程组**杀。

    进程可能早已退出，任何异常都不该往外冒。
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass  # 退回到只杀壳
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # 退回到只杀壳
    try:
        proc.kill()
    except (ProcessLookupError, OSError, RuntimeError):
        pass


def _close_transport(proc: asyncio.subprocess.Process) -> None:
    """显式关掉子进程的管道传输层。

    不关的话，Windows 的 Proactor 传输层要等对象被 GC 才 close，而那通常发生在
    **事件循环关闭之后** —— asyncio 会往 stderr 吐 `RuntimeError: Event loop is closed`，
    在 pytest 里变成 `PytestUnraisableExceptionWarning`（实测噪音）。
    这里用的是 `_transport` 私有字段：asyncio 没有公开的关闭入口，
    而它就在 `asyncio.subprocess.Process` 的文档化实现里，风险可接受。
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception:  # noqa: BLE001 —— 关闭失败只是噪音，不该影响主流程
            pass


@atexit.register
def _kill_all_at_exit() -> None:
    """进程退出兜底：别把 dev server / watch 进程留在用户机器上。"""
    for proc in list(_LIVE_PROCS):
        _kill_quietly(proc)
    _LIVE_PROCS.clear()


@dataclass
class ShellRecord:
    """一个后台 shell 的当前状态。"""

    id: str
    command: str
    proc: asyncio.subprocess.Process
    started_at: float = 0.0
    exit_code: int | None = None
    truncated: bool = False
    _chunks: list[str] = field(default_factory=list)
    _size: int = 0
    _reader: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.exit_code is None

    @property
    def elapsed_s(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def append(self, text: str) -> None:
        """追加输出；超过上限时丢最早的块（保留尾部）。"""
        if not text:
            return
        self._chunks.append(text)
        self._size += len(text)
        while self._size > MAX_BUFFER_CHARS and len(self._chunks) > 1:
            self._size -= len(self._chunks.pop(0))
            self.truncated = True

    def output(self) -> str:
        return "".join(self._chunks)

    def status_line(self) -> str:
        if self.running:
            return f"running（已运行 {self.elapsed_s:.1f}s）"
        return f"exited（退出码 {self.exit_code}）"


class ShellRegistry:
    """一次会话内的后台 shell 集合。"""

    def __init__(self) -> None:
        self._shells: dict[str, ShellRecord] = {}
        self._seq = 0

    # ── 启动 ──────────────────────────────────────────────────────

    async def spawn(
        self,
        command: str,
        *,
        cwd: str | os.PathLike[str],
        env: dict[str, str] | None = None,
    ) -> ShellRecord:
        """起一个后台进程并立刻返回（不等它跑完）。

        输出合并 stdout+stderr 到一个管道，由后台 task 持续读进缓冲 ——
        不读的话管道写满会把子进程**卡死**（这是后台 shell 最经典的坑）。
        """
        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
            "cwd": str(cwd),
            "env": env,
        }
        if sys.platform == "win32":
            # 别弹黑窗；独立进程组便于整组终止
            kwargs["creationflags"] = (
                0x08000000 | 0x00000200
            )  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            # 独立会话/进程组 → 终止时能按组杀，不会漏掉子进程
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_shell(command, **kwargs)

        self._seq += 1
        rec = ShellRecord(
            id=f"shell-{self._seq}",
            command=command,
            proc=proc,
            started_at=time.monotonic(),
        )
        self._shells[rec.id] = rec
        _LIVE_PROCS.add(proc)
        rec._reader = asyncio.create_task(self._pump(rec))
        logger.debug("background shell %s started: %s", rec.id, command)
        return rec

    async def _pump(self, rec: ShellRecord) -> None:
        """把子进程输出读进缓冲，直到 EOF；然后记录退出码。"""
        stream = rec.proc.stdout
        try:
            if stream is not None:
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    rec.append(chunk.decode("utf-8", errors="replace"))
            rec.exit_code = await rec.proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 读失败不该拖垮会话
            logger.debug("background shell %s reader stopped: %s", rec.id, e)
            if rec.exit_code is None:
                rec.exit_code = rec.proc.returncode
        finally:
            _LIVE_PROCS.discard(rec.proc)
            if rec.exit_code is None:
                rec.exit_code = rec.proc.returncode
            _close_transport(rec.proc)

    # ── 查询 / 干预 ────────────────────────────────────────────────

    def get(self, shell_id: str) -> ShellRecord | None:
        return self._shells.get(shell_id)

    def list(self) -> list[ShellRecord]:
        return list(self._shells.values())

    async def wait(self, rec: ShellRecord, wait_ms: int) -> None:
        """等它结束，最多等 `wait_ms` 毫秒；到点就返回当前状态（不报错）。"""
        deadline = min(max(wait_ms, 0), MAX_WAIT_MS) / 1000.0
        if deadline <= 0 or not rec.running:
            return
        try:
            await asyncio.wait_for(asyncio.shield(rec.proc.wait()), timeout=deadline)
        except TimeoutError:
            return
        except Exception:  # noqa: BLE001
            return

    def kill(self, shell_id: str) -> bool:
        """杀一个后台 shell。返回是否真的找到了它（幂等：已退出的也算找到）。"""
        rec = self._shells.get(shell_id)
        if rec is None:
            return False
        _kill_quietly(rec.proc)
        if rec._reader is not None and not rec._reader.done():
            rec._reader.cancel()
        else:
            _close_transport(rec.proc)
        return True

    def kill_all(self) -> None:
        """会话结束时清场（同步、尽力而为）。"""
        for rec in self._shells.values():
            _kill_quietly(rec.proc)
            if rec._reader is not None and not rec._reader.done():
                rec._reader.cancel()
            else:
                _close_transport(rec.proc)
        self._shells.clear()


def describe(rec: ShellRecord) -> str:
    """给工具结果用的一行摘要。"""
    return f"{rec.id}: {rec.status_line()} | {shlex.quote(rec.command)[:120]}"


__all__ = [
    "MAX_BUFFER_CHARS",
    "MAX_WAIT_MS",
    "ShellRecord",
    "ShellRegistry",
    "describe",
]
