"""stdio 传输：通过子进程 stdin/stdout 通信。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from typing import Any

from core.mcp.transport.base import Transport

logger = logging.getLogger(__name__)


class StdioTransport(Transport):
    """通过子进程的 stdin/stdout 发送/接收 JSON-RPC 行。"""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._command = command
        self._args = args or []
        self._env = {**os.environ, **(env or {})}
        self._cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._process is not None and self._process.returncode is None

    async def connect(self) -> None:
        # Resolve executable path (Windows: npx → npx.cmd)
        exe = self._resolve_exe(self._command)
        if not exe:
            raise FileNotFoundError(f"Command not found: {self._command}")

        logger.info("Starting stdio process: %s %s", exe, " ".join(self._args))
        self._process = await asyncio.create_subprocess_exec(
            exe,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
        )
        self._connected = True

    @staticmethod
    def _resolve_exe(command: str) -> str | None:
        """Resolve command to executable path, handling Windows wrappers."""
        # Try direct which() first
        found = shutil.which(command)
        if found:
            return found
        # On Windows, try .cmd / .bat / .exe variants
        if sys.platform == "win32":
            for ext in (".cmd", ".bat", ".exe"):
                found = shutil.which(command + ext)
                if found:
                    return found
        return None

    async def send(self, message: bytes) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("Transport not connected")
        self._process.stdin.write(message)
        await self._process.stdin.drain()

    async def receive(self) -> bytes:
        if not self._process or not self._process.stdout:
            raise RuntimeError("Transport not connected")
        line = await self._process.stdout.readline()
        if not line:
            raise ConnectionError("Process stdout closed")
        return line

    async def close(self) -> None:
        self._connected = False
        if not self._process:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
        except Exception:
            pass
        self._process = None

    async def _read_stderr(self) -> str:
        """读取子进程 stderr（用于调试）。"""
        if not self._process or not self._process.stderr:
            return ""
        try:
            data = await asyncio.wait_for(self._process.stderr.read(), timeout=0.5)
            return data.decode(errors="replace") if data else ""
        except asyncio.TimeoutError:
            return ""
