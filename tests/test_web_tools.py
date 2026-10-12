"""联网工具 —— `web_fetch` / `web_search` 与 SSRF 守卫。

测试策略：**不依赖公网**。所有抓取都打一个本地起的 HTTP 服务（`web_server`
fixture），所以离线也能跑、结果确定。SSRF 守卫单独做单元测试（不需要网络）。

只有一条 `test_fetch_real_public_page` 会真的联网，且连不上就 skip ——
它是"整条链路真的能用"的证据，不该让套件因为它而变脆。
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.tool.context import ExecutionContext
from core.tool.tools.web_tools import WebFetchTool, WebSearchTool
from core.tool.web import (
    BlockedURLError,
    check_url_allowed,
    html_to_text,
    page_title,
    parse_search_results,
    unwrap_redirect,
)

_DOC_HTML = """<html><head><title>Doc Title</title>
<style>body{color:red}</style></head><body>
<h1>Heading</h1><script>alert('x')</script>
<p>Some <b>bold</b> text &amp; an entity.</p>
<ul><li>first</li><li>second</li></ul>
</body></html>"""

# DuckDuckGo html 版的骨架（标题锚点 + 紧随其后的摘要锚点）
_SERP_HTML = """<html><body>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">asyncio docs</a>
  <a class="result__snippet">Event loop, tasks, coroutines.</a>
</div>
<div class="result">
  <a class="result__a" href="https://realpython.com/async-io-python/">Async IO in Python</a>
  <a class="result__snippet">A complete walkthrough.</a>
</div>
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
    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/doc":
            self._send(200, _DOC_HTML, "text/html; charset=utf-8")
        elif path == "/plain":
            self._send(200, "line one\nline two\n", "text/plain")
        elif path == "/big":
            self._send(200, "A" * 60_000, "text/plain")
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/doc")
            self.end_headers()
        elif path == "/boom":
            self._send(500, "kaboom", "text/plain")
        elif path == "/search":
            self._send(200, _SERP_HTML, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def _send(self, code: int, body: str, ctype: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def web_server():
    """本地 HTTP 服务；返回 `http://127.0.0.1:<port>`。"""
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


@pytest.fixture
def allow_private(monkeypatch):
    """本地服务是环回地址 —— 测试里显式放开守卫（默认是拒的，见 SSRF 用例）。"""
    monkeypatch.setenv("CODEFORGE_WEB_ALLOW_PRIVATE", "1")


# ── SSRF 守卫（不需要网络）────────────────────────────────────────


class TestSSRFGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080/",
            "http://localhost:3000/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # 云元数据
            "http://[::1]/",
            "http://0.0.0.0/",
        ],
    )
    async def test_private_and_loopback_are_refused(self, url, monkeypatch):
        monkeypatch.delenv("CODEFORGE_WEB_ALLOW_PRIVATE", raising=False)
        with pytest.raises(BlockedURLError):
            await check_url_allowed(url)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "gopher://x"])
    async def test_non_http_schemes_are_refused(self, url):
        with pytest.raises(BlockedURLError):
            await check_url_allowed(url)

    @pytest.mark.asyncio
    async def test_public_host_is_allowed(self, monkeypatch):
        monkeypatch.delenv("CODEFORGE_WEB_ALLOW_PRIVATE", raising=False)
        assert (
            await check_url_allowed("https://example.com/x") == "https://example.com/x"
        )

    @pytest.mark.asyncio
    async def test_unresolvable_host_is_refused(self):
        with pytest.raises(BlockedURLError):
            await check_url_allowed("http://this-host-does-not-exist.invalid/")

    @pytest.mark.asyncio
    async def test_explicit_optin_allows_private(self, monkeypatch):
        """显式开 `CODEFORGE_WEB_ALLOW_PRIVATE=1` 才放行私网（默认关）。"""
        monkeypatch.setenv("CODEFORGE_WEB_ALLOW_PRIVATE", "1")
        assert await check_url_allowed("http://127.0.0.1:9/") == "http://127.0.0.1:9/"


# ── HTML → 文本 ───────────────────────────────────────────────────


class TestHtmlToText:
    def test_drops_script_and_style_keeps_text(self):
        text = html_to_text(_DOC_HTML)
        assert "Heading" in text
        assert "bold" in text
        assert "alert(" not in text  # script 内容丢掉
        assert "color:red" not in text  # style 内容丢掉

    def test_decodes_entities_and_breaks_blocks(self):
        text = html_to_text(_DOC_HTML)
        assert "&" in text and "&amp;" not in text
        assert "\n" in text  # 块级标签要断行，不能挤成一坨

    def test_title(self):
        assert page_title(_DOC_HTML) == "Doc Title"

    def test_malformed_html_does_not_raise(self):
        assert html_to_text("<p>unclosed <b>bold").startswith("unclosed")


# ── 搜索结果解析 ──────────────────────────────────────────────────


class TestSearchParsing:
    def test_parses_duckduckgo_layout(self):
        hits = parse_search_results(_SERP_HTML, limit=5)
        assert len(hits) == 2
        assert hits[0].title == "asyncio docs"
        assert hits[0].url == "https://docs.python.org/3/library/asyncio.html"
        assert hits[0].snippet == "Event loop, tasks, coroutines."

    def test_unwraps_duckduckgo_redirect(self):
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1"
        assert unwrap_redirect(wrapped) == "https://example.com/a?b=1"

    def test_generic_fallback_for_unknown_layout(self):
        """认不出结构化标记时退化成"所有外链都是结果"，而不是返回空。"""
        html = '<a href="https://a.example/x">First result</a>'
        hits = parse_search_results(html, limit=5)
        assert len(hits) == 1
        assert hits[0].url == "https://a.example/x"

    def test_respects_limit(self):
        html = "".join(f'<a href="https://e.example/{i}">r{i}</a>' for i in range(10))
        assert len(parse_search_results(html, limit=3)) == 3


# ── web_fetch 工具 ────────────────────────────────────────────────


class TestWebFetch:
    @pytest.mark.asyncio
    async def test_fetches_and_converts_html(self, ctx, web_server, allow_private):
        r = await WebFetchTool().execute(ctx, {"url": f"{web_server}/doc"})
        assert r.success is True, r.error
        assert "Heading" in r.data
        assert "alert(" not in r.data
        assert r.meta["status"] == 200

    @pytest.mark.asyncio
    async def test_plain_text_passthrough(self, ctx, web_server, allow_private):
        r = await WebFetchTool().execute(ctx, {"url": f"{web_server}/plain"})
        assert r.success is True
        assert "line one" in r.data

    @pytest.mark.asyncio
    async def test_follows_redirect(self, ctx, web_server, allow_private):
        r = await WebFetchTool().execute(ctx, {"url": f"{web_server}/redirect"})
        assert r.success is True
        assert "Heading" in r.data
        assert r.meta["url"].endswith("/doc")  # 报告的是最终地址

    @pytest.mark.asyncio
    async def test_truncates_to_max_chars(self, ctx, web_server, allow_private):
        r = await WebFetchTool().execute(
            ctx, {"url": f"{web_server}/big", "max_chars": 1000}
        )
        assert r.success is True
        assert "未显示" in r.data
        assert len(r.data) < 2000

    @pytest.mark.asyncio
    async def test_http_error_is_reported_not_raised(
        self, ctx, web_server, allow_private
    ):
        r = await WebFetchTool().execute(ctx, {"url": f"{web_server}/boom"})
        assert r.success is False
        assert "500" in (r.error or "")

    @pytest.mark.asyncio
    async def test_blocked_url_reports_reason(self, ctx, monkeypatch):
        monkeypatch.delenv("CODEFORGE_WEB_ALLOW_PRIVATE", raising=False)
        r = await WebFetchTool().execute(ctx, {"url": "http://127.0.0.1:9/"})
        assert r.success is False
        assert r.meta["blocked"] is True
        assert "非公网" in r.error

    @pytest.mark.asyncio
    async def test_connection_failure_is_reported(self, ctx, allow_private):
        """端口没人听 → 报错，不抛。"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        r = await WebFetchTool().execute(ctx, {"url": f"http://127.0.0.1:{port}/"})
        assert r.success is False
        assert "抓取失败" in r.error

    def test_metadata(self):
        t = WebFetchTool()
        assert t.name() == "web_fetch"
        assert t.category() == "command"  # 出站请求要过审批，见模块 docstring
        assert t.is_read_only() is True
        assert t.is_destructive() is False
        assert t.max_retries == 0


# ── web_search 工具 ───────────────────────────────────────────────


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_parses_results_from_backend(
        self, ctx, web_server, allow_private, monkeypatch
    ):
        monkeypatch.setenv("CODEFORGE_SEARCH_URL", f"{web_server}/search?q={{query}}")
        r = await WebSearchTool().execute(ctx, {"query": "asyncio"})
        assert r.success is True, r.error
        assert r.meta["count"] == 2
        assert "asyncio docs" in r.data
        assert "https://docs.python.org/3/library/asyncio.html" in r.data

    @pytest.mark.asyncio
    async def test_query_is_url_encoded(
        self, ctx, web_server, allow_private, monkeypatch
    ):
        """搜索词要编码 —— 空格/中文直接拼进 URL 会打歪。"""
        monkeypatch.setenv("CODEFORGE_SEARCH_URL", f"{web_server}/search?q={{query}}")
        r = await WebSearchTool().execute(ctx, {"query": "中文 空格"})
        assert r.success is True  # 服务端只认 /search，编码错了会 404 → 就不是 True

    @pytest.mark.asyncio
    async def test_backend_failure_is_actionable(
        self, ctx, web_server, allow_private, monkeypatch
    ):
        """后端不可达时要告诉用户怎么换后端，而不是干巴巴报错。"""
        monkeypatch.setenv("CODEFORGE_SEARCH_URL", f"{web_server}/boom?q={{query}}")
        r = await WebSearchTool().execute(ctx, {"query": "x"})
        assert r.success is False
        assert "HTTP 500" in r.error

    @pytest.mark.asyncio
    async def test_unreachable_backend_mentions_override(
        self, ctx, allow_private, monkeypatch
    ):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        monkeypatch.setenv(
            "CODEFORGE_SEARCH_URL", f"http://127.0.0.1:{port}/s?q={{query}}"
        )
        r = await WebSearchTool().execute(ctx, {"query": "x"})
        assert r.success is False
        assert "CODEFORGE_SEARCH_URL" in r.error

    @pytest.mark.asyncio
    async def test_template_without_placeholder_is_rejected(self, ctx, monkeypatch):
        monkeypatch.setenv("CODEFORGE_SEARCH_URL", "https://example.com/search")
        r = await WebSearchTool().execute(ctx, {"query": "x"})
        assert r.success is False
        assert "{query}" in r.error

    @pytest.mark.asyncio
    async def test_empty_query_rejected(self, ctx):
        r = await WebSearchTool().execute(ctx, {"query": "   "})
        assert r.success is False

    def test_metadata(self):
        t = WebSearchTool()
        assert t.name() == "web_search"
        assert t.category() == "command"
        assert t.max_retries == 0


# ── 真链路（连不上就跳过）────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_real_public_page(ctx, monkeypatch):
    """真去抓一个公网页面 —— 证明整条链路（DNS/守卫/TLS/解码/转文本）真的通。

    连不上（离线/被墙）就 skip：这条是证据，不该让套件变脆。
    """
    monkeypatch.delenv("CODEFORGE_WEB_ALLOW_PRIVATE", raising=False)
    try:
        r = await WebFetchTool().execute(ctx, {"url": "https://example.com"})
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"公网不可达：{e}")
    if not r.success:
        pytest.skip(f"公网不可达：{r.error}")
    assert "documentation examples" in r.data.lower()
