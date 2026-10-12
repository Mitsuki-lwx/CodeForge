"""后台 shell —— `bash(run_in_background)` / `bash_output` / `kill_shell`。

覆盖的是**可观测行为**：起得来、读得到、停得掉、会话结束不留进程。
用解释器自身当被测进程（`sys.executable -c ...`），不依赖任何 Unix 工具，
所以 Windows / Linux 都能跑。

⚠️ 子进程的 stdout 是管道 → Python 默认**块缓冲**，输出不会立刻出现。
测试里统一加 `-u`（等价于 `PYTHONUNBUFFERED=1`），这也是给 agent 的提示。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

import pytest

from core.tool.context import ExecutionContext
from core.tool.shells import MAX_BUFFER_CHARS, ShellRegistry
from core.tool.tools import get_default_registry
from core.tool.tools.bash import BashTool
from core.tool.tools.bash_bg import BashOutputTool, KillShellTool


@pytest.fixture
async def ctx(tmp_path):
    """每个测试一个独立 context；**收尾时必须把后台进程收干净**。

    不收的话，`Process` 对象会在事件循环关闭后才被 GC，asyncio 会往 stderr 吐
    `RuntimeError: Event loop is closed` —— 不影响断言，但会把 CI 输出搅浑。
    """
    c = ExecutionContext(cwd=tmp_path, session_id="test")
    try:
        yield c
    finally:
        procs = [r.proc for r in c.shells.list()]
        c.shells.kill_all()
        for p in procs:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(p.wait(), timeout=10)


@pytest.fixture
def bash():
    return BashTool()


def _py(code: str) -> str:
    """用解释器跑一段代码（带 -u，避免 stdout 缓冲）。"""
    return f'"{sys.executable}" -u -c "{code}"'


class TestSpawnBackground:
    @pytest.mark.asyncio
    async def test_returns_shell_id_without_waiting(self, bash, ctx):
        """★ 起一个会跑很久的进程，调用必须**立刻**返回，不阻塞。"""
        result = await asyncio.wait_for(
            bash.execute(
                ctx,
                {
                    "command": _py("import time; time.sleep(30)"),
                    "run_in_background": True,
                },
            ),
            timeout=10,
        )
        assert result.success is True, result.error
        shell_id = result.meta["shell_id"]
        assert shell_id.startswith("shell-")
        assert ctx.shells.get(shell_id) is not None
        assert ctx.shells.get(shell_id).running is True

    @pytest.mark.asyncio
    async def test_foreground_default_is_unchanged(self, bash, ctx):
        """不加 run_in_background 时行为与改动前一致（同步返回输出）。"""
        result = await bash.execute(ctx, {"command": "echo hi"})
        assert result.success is True
        assert "hi" in result.data
        assert ctx.shells.list() == []

    @pytest.mark.asyncio
    async def test_spawn_failure_is_reported(self, ctx):
        """起不来要报清楚，不能抛。"""
        reg = ShellRegistry()
        with pytest.raises(OSError):
            await reg.spawn("echo x", cwd="Z:/definitely/not/here")


class TestBashOutput:
    @pytest.mark.asyncio
    async def test_reads_output_and_exit_code(self, bash, ctx):
        """★ 进程结束后能读到输出与退出码。"""
        out = BashOutputTool()
        await bash.execute(
            ctx,
            {"command": _py("print('hello-bg')"), "run_in_background": True},
        )
        shell_id = ctx.shells.list()[0].id

        r = await out.execute(ctx, {"shell_id": shell_id, "wait_ms": 10_000})
        assert r.success is True, r.error
        assert "hello-bg" in r.data
        assert r.meta["status"] == "exited"
        assert r.meta["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_wait_ms_returns_early_while_still_running(self, bash, ctx):
        """还在跑时不能傻等 —— wait_ms 到点就返回 running。"""
        out = BashOutputTool()
        await bash.execute(
            ctx,
            {"command": _py("import time; time.sleep(30)"), "run_in_background": True},
        )
        shell_id = ctx.shells.list()[0].id
        r = await out.execute(ctx, {"shell_id": shell_id, "wait_ms": 300})
        assert r.success is True
        assert r.meta["status"] == "running"

    @pytest.mark.asyncio
    async def test_unknown_shell_id_lists_known_ones(self, bash, ctx):
        """给错 id 要报错，并把当前有哪些列出来（省一轮来回）。"""
        out = BashOutputTool()
        await bash.execute(
            ctx,
            {"command": _py("import time; time.sleep(30)"), "run_in_background": True},
        )
        known = ctx.shells.list()[0].id
        r = await out.execute(ctx, {"shell_id": "shell-999"})
        assert r.success is False
        assert "shell-999" in r.error
        assert known in r.error

    @pytest.mark.asyncio
    async def test_output_is_tail_truncated(self, bash, ctx):
        """超长输出只返回尾部，并说明省略了多少。"""
        out = BashOutputTool()
        await bash.execute(
            ctx,
            {
                "command": _py("print('A'*5000); print('TAIL-MARKER')"),
                "run_in_background": True,
            },
        )
        shell_id = ctx.shells.list()[0].id
        r = await out.execute(
            ctx, {"shell_id": shell_id, "wait_ms": 10_000, "max_chars": 100}
        )
        assert r.success is True
        assert "TAIL-MARKER" in r.data  # 保留的是尾部
        assert "已省略" in r.data

    def test_output_property_is_read_only(self):
        out = BashOutputTool()
        assert out.is_read_only() is True
        assert out.category() == "read"
        assert out.max_retries == 0


class TestKillShell:
    @pytest.mark.asyncio
    async def test_kills_running_process(self, bash, ctx):
        """★ 停掉后进程真的没了（不是只把记录删了）。"""
        kill = KillShellTool()
        await bash.execute(
            ctx,
            {"command": _py("import time; time.sleep(30)"), "run_in_background": True},
        )
        rec = ctx.shells.list()[0]
        r = await kill.execute(ctx, {"shell_id": rec.id})
        assert r.success is True
        assert r.meta["was_running"] is True

        await asyncio.wait_for(rec.proc.wait(), timeout=10)
        assert rec.proc.returncode is not None

    @pytest.mark.asyncio
    async def test_kill_unknown_id_is_an_error(self, ctx):
        kill = KillShellTool()
        r = await kill.execute(ctx, {"shell_id": "shell-404"})
        assert r.success is False
        assert "shell-404" in r.error

    @pytest.mark.asyncio
    async def test_kill_after_exit_is_ok(self, bash, ctx):
        """进程自己退了之后再 kill 不算失败（幂等）。"""
        kill = KillShellTool()
        await bash.execute(
            ctx, {"command": _py("print('done')"), "run_in_background": True}
        )
        rec = ctx.shells.list()[0]
        await ctx.shells.wait(rec, 10_000)
        r = await kill.execute(ctx, {"shell_id": rec.id})
        assert r.success is True
        assert r.meta["was_running"] is False

    def test_kill_is_destructive_command_category(self):
        kill = KillShellTool()
        assert kill.is_read_only() is False
        assert kill.is_destructive() is True
        assert kill.category() == "shell"
        assert kill.max_retries == 0


class TestRegistry:
    @pytest.mark.asyncio
    async def test_kill_all_stops_everything(self, bash, ctx):
        """会话结束清场：所有还活着的都要被杀掉。"""
        for _ in range(2):
            await bash.execute(
                ctx,
                {
                    "command": _py("import time; time.sleep(30)"),
                    "run_in_background": True,
                },
            )
        procs = [r.proc for r in ctx.shells.list()]
        assert len(procs) == 2
        ctx.shells.kill_all()
        for p in procs:
            await asyncio.wait_for(p.wait(), timeout=10)
            assert p.returncode is not None

    @pytest.mark.asyncio
    async def test_buffer_caps_and_keeps_tail(self, bash, ctx):
        """输出超过上限时丢最早的、留尾部，并置 truncated。"""
        await bash.execute(
            ctx,
            {
                "command": _py(f"print('H'*{MAX_BUFFER_CHARS + 50_000})"),
                "run_in_background": True,
            },
        )
        rec = ctx.shells.list()[0]
        await ctx.shells.wait(rec, 20_000)
        assert rec.truncated is True
        assert len(rec.output()) <= MAX_BUFFER_CHARS

    @pytest.mark.asyncio
    async def test_session_close_kills_background_shells(self, tmp_path):
        """★ `SessionBundle.close()` 要把后台进程带走（不然 dev server 会残留）。"""
        from tests.test_session_bootstrap import _build

        bundle = await _build(tmp_path)
        try:
            ctx = bundle.agent._exec_ctx
            await BashTool().execute(
                ctx,
                {
                    "command": _py("import time; time.sleep(30)"),
                    "run_in_background": True,
                },
            )
            proc = ctx.shells.list()[0].proc
            assert proc.returncode is None
        finally:
            bundle.close()
        await asyncio.wait_for(proc.wait(), timeout=10)
        assert proc.returncode is not None


class TestToolRegistration:
    def test_default_registry_exposes_the_three(self):
        reg = get_default_registry()
        assert reg.get("bash_output").name() == "bash_output"
        assert reg.get("kill_shell").name() == "kill_shell"

    def test_bash_schema_advertises_background(self):
        schema = BashTool().input_schema()
        assert "run_in_background" in schema["properties"]


class TestVerifyAServiceEndToEnd:
    """★ 这是这套能力的**目的**：起一个服务 → 验证它真的响应 → 关掉它。

    用本地 TCP 服务（绑 0 端口，把选中的端口打印出来）而不是固定端口：
    固定端口会跟机器上别的东西撞，测试就变成"看运气"。
    """

    _SERVER = """\
import http.server, socketserver, functools
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=".")
srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
print(srv.server_address[1], flush=True)   # 把选中的端口报出来
srv.serve_forever()
"""

    @pytest.mark.asyncio
    async def test_start_verify_stop(self, bash, ctx, tmp_path):
        (tmp_path / "index.html").write_text("<h1>OK-MARKER</h1>", encoding="utf-8")
        (tmp_path / "serve.py").write_text(self._SERVER, encoding="utf-8")

        out_tool = BashOutputTool()
        kill_tool = KillShellTool()

        # 1) 起服务（后台）。直接跑脚本文件而不是 `-c "..."`：
        #    命令里有引号时 cmd.exe 的转义规则会咬人（实测踩过）。
        started = await bash.execute(
            ctx,
            {
                "command": f'"{sys.executable}" -u serve.py',
                "run_in_background": True,
            },
        )
        assert started.success is True, started.error
        shell_id = started.meta["shell_id"]

        # 2) 等它报出端口
        port = None
        for _ in range(20):
            r = await out_tool.execute(ctx, {"shell_id": shell_id, "wait_ms": 500})
            digits = [w for w in r.data.split() if w.isdigit()]
            if digits:
                port = int(digits[-1])
                break
        assert port, f"服务没报出端口：{r.data!r}"

        # 3) 验证它**真的**在响应（不是"看起来起来了"）
        probe = (
            "import urllib.request;"
            f"print(urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=10)"
            ".read().decode())"
        )
        got = await bash.execute(ctx, {"command": _py(probe)})
        assert got.success is True, got.error
        assert "OK-MARKER" in got.data

        # 4) 关掉它，并确认进程真的没了
        rec = ctx.shells.get(shell_id)
        killed = await kill_tool.execute(ctx, {"shell_id": shell_id})
        assert killed.success is True
        await asyncio.wait_for(rec.proc.wait(), timeout=10)
        assert rec.proc.returncode is not None

        # 5) 服务真的停了：端口上**最终**连不上了。
        #
        #    刻意轮询而不是"立刻断言"：SIGKILL 是异步的 —— `proc.wait()` 返回的是
        #    **壳进程**被回收，被杀的子进程释放监听套接字可能晚几毫秒。
        #    立刻断言就是拿调度运气当契约（实测在 Linux 上偶发假失败）。
        #    也刻意不用"再 bind 一次"：Windows 上监听套接字关闭后会进 TIME_WAIT，
        #    bind 会假失败（WinError 10048）；connect 被拒才是"没有监听者"。
        import socket as _socket

        deadline = time.monotonic() + 10
        still_listening = True
        while time.monotonic() < deadline:
            probe_sock = _socket.socket()
            probe_sock.settimeout(2)
            try:
                probe_sock.connect(("127.0.0.1", port))
                still_listening = True
            except OSError:
                still_listening = False
            finally:
                probe_sock.close()
            if not still_listening:
                break
            time.sleep(0.1)
        assert not still_listening, f"端口 {port} 还有监听者 —— 进程没被真正停掉"
