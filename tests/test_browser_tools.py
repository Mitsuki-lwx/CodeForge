"""浏览器工具 —— 用真实 Chromium 验证页面渲染。

测试策略：本地起一个 HTTP 服务当被测站点（不依赖公网），用真浏览器打开它。
这样验的是**整条链路**：启动 Chromium → 导航 → 读 DOM/控制台 → 截图。

没装 playwright / 没下 Chromium 时整体 skip —— 浏览器是可选依赖，
不该让没装它的人跑不了测试套件。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from core.tool.tools.browser_tools import (
    BrowserDiffTool,
    BrowserEvalTool,
    BrowserScreenshotTool,
    BrowserSnapshotTool,
)

playwright = pytest.importorskip("playwright", reason="未安装可选依赖 playwright")

from playwright.async_api import async_playwright  # noqa: E402


def _browser_available() -> bool:
    """Chromium 内核真的下好了吗（装了包 ≠ 下了内核）。"""
    import asyncio

    from core.tool.browser import _launch

    async def _probe() -> bool:
        async with async_playwright() as pw:
            try:
                b = await _launch(pw)
            except Exception:  # noqa: BLE001
                return False
            await b.close()
            return True

    try:
        return asyncio.run(_probe())
    except Exception:  # noqa: BLE001
        return False


#: 需要真浏览器的用例才打这个标记 —— 元数据/错误路径的用例没内核也该跑。
needs_browser = pytest.mark.skipif(
    not _browser_available(),
    reason="未安装 Chromium 内核（playwright install chromium）",
)


_PAGE = """<!doctype html><html><head><title>Todo App</title></head><body>
<h1>My Todos</h1>
<ul id="list"><li class="todo-item">buy milk</li><li class="todo-item">write tests</li></ul>
<button id="add" onclick="document.getElementById('list').insertAdjacentHTML('beforeend','<li class=\\'todo-item\\'>clicked</li>')">Add</button>
<script>console.log('page-ready'); console.warn('be careful');</script>
</body></html>"""

_LONG = (
    "<!doctype html><html><head><title>Long</title></head><body><p>"
    + ("filler sentence. " * 200)
    + "</p></body></html>"
)

_BROKEN = """<!doctype html><html><head><title>Broken</title></head><body>
<h1>Broken page</h1>
<script>throw new Error('boom-from-page');</script>
</body></html>"""


#: Chromium 有一张**禁用端口**表（SIP 5060、IRC 6665-6669、6000 等），
#: 往这些端口导航会直接 `net::ERR_UNSAFE_PORT`。
#: 测试用 `bind(("127.0.0.1", 0))` 拿随机端口，撞上它们的概率不高但**真的会撞**
#: （实测撞到 5060）。撞了就是"随机失败"，所以这里显式避开。
_CHROMIUM_BLOCKED_PORTS = frozenset(
    {
        2049,
        3659,
        4045,
        5060,
        5061,
        6000,
        6566,
        6665,
        6666,
        6667,
        6668,
        6669,
        6697,
        10080,
    }
)


def _safe_free_port() -> int:
    """拿一个 Chromium 允许访问的随机空闲端口。"""
    import socket

    for _ in range(20):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        if port not in _CHROMIUM_BLOCKED_PORTS:
            return port
    raise RuntimeError("拿不到可用的随机端口")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/broken":
            body = _BROKEN
        elif path == "/long":
            body = _LONG
        else:
            body = _PAGE
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def site():
    httpd = ThreadingHTTPServer(("127.0.0.1", _safe_free_port()), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def ctx(tmp_path):
    return ExecutionContext(cwd=tmp_path, session_id="test")


@needs_browser
class TestBrowserSnapshot:
    @pytest.mark.asyncio
    async def test_reports_title_and_visible_text(self, ctx, site):
        """★ 打开页面能读回**真实渲染**的文本，不是源码。"""
        r = await BrowserSnapshotTool().execute(ctx, {"url": f"{site}/"})
        assert r.success is True, r.error
        assert "My Todos" in r.data
        assert "buy milk" in r.data
        assert r.meta["title"] == "Todo App"
        assert r.meta["status"] == 200

    @pytest.mark.asyncio
    async def test_console_output_is_captured(self, ctx, site):
        """控制台输出是"页面真的跑起来了"的证据。"""
        r = await BrowserSnapshotTool().execute(ctx, {"url": f"{site}/"})
        assert "page-ready" in r.data
        assert "be careful" in r.data

    @pytest.mark.asyncio
    async def test_page_errors_are_surfaced(self, ctx, site):
        """★ 未捕获异常要显眼报出来 —— 这正是"验证"要抓的东西。"""
        r = await BrowserSnapshotTool().execute(ctx, {"url": f"{site}/broken"})
        assert r.success is True
        assert "boom-from-page" in r.data
        assert r.meta["errors"] >= 1

    @pytest.mark.asyncio
    async def test_js_interaction_changes_the_result(self, ctx, site):
        """★ 传 js 先点击，再读结果 —— 验证交互后的状态。"""
        js = "document.getElementById('add').click()"
        r = await BrowserSnapshotTool().execute(ctx, {"url": f"{site}/", "js": js})
        assert r.success is True, r.error
        assert "clicked" in r.data

    @pytest.mark.asyncio
    async def test_unreachable_url_is_reported_not_raised(self, ctx):
        r = await BrowserSnapshotTool().execute(ctx, {"url": "http://127.0.0.1:1/"})
        assert r.success is False
        assert "打开页面失败" in r.error

    @pytest.mark.asyncio
    async def test_max_chars_truncates(self, ctx, site):
        # 用长页面：短页面的文本本来就不足上限，测不出截断
        r = await BrowserSnapshotTool().execute(
            ctx, {"url": f"{site}/long", "max_chars": 200}
        )
        assert r.success is True
        assert "未显示" in r.data


@needs_browser
class TestBrowserEval:
    @pytest.mark.asyncio
    async def test_returns_js_value_for_assertions(self, ctx, site):
        """★ 断言通道：数一数页面上有几个元素。"""
        r = await BrowserEvalTool().execute(
            ctx,
            {"url": f"{site}/", "js": "document.querySelectorAll('.todo-item').length"},
        )
        assert r.success is True, r.error
        assert r.data.strip().endswith("2")

    @pytest.mark.asyncio
    async def test_returns_objects_as_json(self, ctx, site):
        r = await BrowserEvalTool().execute(
            ctx, {"url": f"{site}/", "js": "({title: document.title, n: 1})"}
        )
        assert r.success is True
        assert '"Todo App"' in r.data

    @pytest.mark.asyncio
    async def test_js_error_is_reported(self, ctx, site):
        r = await BrowserEvalTool().execute(
            ctx, {"url": f"{site}/", "js": "throw new Error('bad-js')"}
        )
        assert r.success is False
        assert "bad-js" in r.error


@needs_browser
class TestBrowserScreenshot:
    @pytest.mark.asyncio
    async def test_saves_png_inside_workspace(self, ctx, site, tmp_path):
        """★ 截图落到工作区内的 `.codeforge/screenshots/`，且是真 PNG。"""
        r = await BrowserScreenshotTool().execute(
            ctx, {"url": f"{site}/", "label": "todo"}
        )
        assert r.success is True, r.error
        path = Path(r.meta["screenshot"])
        assert path.exists()
        assert path.parent == tmp_path / ".codeforge" / "screenshots"
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.asyncio
    async def test_no_arbitrary_output_path(self, ctx, site):
        """刻意不接受输出路径 —— 那等于开一条绕过路径沙箱的写通道。"""
        schema = BrowserScreenshotTool().input_schema()
        assert "path" not in schema["properties"]

    @pytest.mark.asyncio
    async def test_full_page_option(self, ctx, site):
        r = await BrowserScreenshotTool().execute(
            ctx, {"url": f"{site}/", "full_page": True}
        )
        assert r.success is True
        assert Path(r.meta["screenshot"]).exists()


class TestToolMetadata:
    @pytest.mark.parametrize(
        "tool_cls",
        [BrowserSnapshotTool, BrowserEvalTool, BrowserScreenshotTool],
    )
    def test_category_and_retries(self, tool_cls):
        t = tool_cls()
        assert t.category() == "command"  # 能摸内网 → 要过审批
        assert t.max_retries == 0
        assert t.is_destructive() is False

    def test_registered_in_default_registry(self):
        reg = get_default_registry()
        for name in ("browser_snapshot", "browser_eval", "browser_screenshot"):
            assert reg.get(name).name() == name


class TestNoPlaywrightInstalled:
    """没装可选依赖时要给一句**能照做**的错误，而不是 ImportError 崩掉。"""

    @pytest.mark.asyncio
    async def test_missing_dependency_is_actionable(self, ctx, monkeypatch):
        import sys

        # 让 `from playwright.async_api import ...` 抛 ImportError
        monkeypatch.setitem(sys.modules, "playwright.async_api", None)
        r = await BrowserSnapshotTool().execute(ctx, {"url": "http://127.0.0.1:1/"})
        assert r.success is False
        assert "codeforge[browser]" in r.error
        assert "playwright install chromium" in r.error

    @pytest.mark.asyncio
    async def test_visit_raises_typed_error(self, monkeypatch):
        import sys

        from core.tool.browser import BrowserUnavailableError, visit

        monkeypatch.setitem(sys.modules, "playwright.async_api", None)
        with pytest.raises(BrowserUnavailableError):
            await visit("http://127.0.0.1:1/")


@needs_browser
class TestBrowserDiff:
    """★ 「交互之后界面真的变了吗」—— 文本断言答不了的那个问题。"""

    @pytest.mark.asyncio
    async def test_click_that_changes_ui_reports_pixels(self, ctx, site):
        r = await BrowserDiffTool().execute(
            ctx,
            {
                "url": f"{site}/",
                "js": "document.getElementById('add').click()",
                "label": "after-click",
            },
        )
        assert r.success is True, r.error
        assert r.meta["changed_pixels"] > 0
        assert "有可见变化" in r.data
        assert 0 < r.meta["ratio"] <= 1

    @pytest.mark.asyncio
    async def test_no_op_interaction_reports_zero(self, ctx, site):
        """★ 点了但什么都没发生 → 必须报 0。这正是"日志里有调用、界面没变"的 bug。"""
        r = await BrowserDiffTool().execute(
            ctx, {"url": f"{site}/", "js": "void 0", "label": "noop"}
        )
        assert r.success is True, r.error
        assert r.meta["changed_pixels"] == 0
        assert "没有可见变化" in r.data

    @pytest.mark.asyncio
    async def test_saves_before_after_and_diff_images(self, ctx, site, tmp_path):
        r = await BrowserDiffTool().execute(
            ctx,
            {
                "url": f"{site}/",
                "js": "document.getElementById('add').click()",
                "label": "shots",
            },
        )
        assert r.success is True
        shots = sorted((tmp_path / ".codeforge" / "screenshots").glob("*.png"))
        assert len(shots) == 3  # before / after / diff
        for p in shots:
            assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.asyncio
    async def test_diff_meta_is_assertable(self, ctx, site):
        """`meta` 里要有可断言的数字（agent 能据此判断，而不是靠读描述）。"""
        r = await BrowserDiffTool().execute(
            ctx,
            {
                "url": f"{site}/",
                "js": "document.getElementById('add').click()",
            },
        )
        assert set(r.meta) >= {"changed_pixels", "total_pixels", "ratio"}
        assert r.meta["total_pixels"] == 1280 * 800

    def test_registered_and_gated(self):
        t = BrowserDiffTool()
        assert t.name() == "browser_diff"
        assert t.category() == "command"
        assert t.max_retries == 0
        assert get_default_registry().get("browser_diff").name() == "browser_diff"
