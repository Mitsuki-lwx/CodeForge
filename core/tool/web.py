"""抓网页 / 搜索的共用底座 —— HTTP 抓取、HTML 转文本、SSRF 守卫。

**为什么单独一个模块**：`web_fetch` 与 `web_search` 共享三件事 ——
① 同一个抓取器（超时、字节上限、重定向逐跳校验）；
② 同一套 SSRF 守卫；
③ 同一个 HTML→文本转换。
写在工具里会复制三份，而这类安全逻辑复制一份就等于多一个漏点。

## SSRF 守卫在防什么

agent 会自己拼 URL。如果不管，它能用这个工具去摸**内网**：
`http://127.0.0.1:8080/admin`、`http://169.254.169.254/latest/meta-data/`（云元数据）、
`http://192.168.1.1/`。这不是"agent 使坏"，而是**抓来的网页里可能写着让 agent
去访问内网**的指令（prompt injection 的标准打法）。

判据：只允许 `http`/`https`；主机名解析出的**每一个** IP 都必须是
`ipaddress.is_global`（公网）—— 环回/私网/链路本地/保留段一律拒。
重定向**逐跳**校验：不能拿一个公网 URL 302 到内网。

⚠️ 残留风险（写在明处，不假装它滴水不漏）：解析与 httpx 实际连接之间存在
TOCTOU 窗口，DNS rebinding 理论上可以钻。要根治得把连接固定到已校验的 IP
（`Host` 头保留域名）。当前实现挡住的是"直接写内网地址"这一类，这是绝大多数
情况；真要更严，应在容器里跑、由网络层兜底。
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

#: 单次抓取的字节上限。默认 2MB —— 够读文档页，又不会被 `pypi.org/simple/`
#: 那种 47MB 的页面拖死（实测）。
WEB_MAX_BYTES = 2_000_000

#: 单次抓取的超时（秒）。
WEB_TIMEOUT = 20.0

#: 最多跟随几次重定向。
MAX_REDIRECTS = 5

#: 有 UA 的请求更容易被站点正常对待（不少站点对空 UA 直接 403）。
USER_AGENT = (
    "Mozilla/5.0 (compatible; CodeForge/0.1; +https://github.com/Mitsuki-lwx/CodeForge)"
)


class BlockedURLError(ValueError):
    """URL 被 SSRF 守卫拦下。"""


@dataclass
class FetchResult:
    """一次抓取的结果。"""

    status: int
    url: str  # 最终 URL（跟随重定向之后）
    text: str
    content_type: str
    truncated: bool


# ── URL 守卫 ──────────────────────────────────────────────────────


def _is_global_ip(ip: str) -> bool:
    """这个 IP 是不是公网地址。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # `is_global` 已经把环回/私网/链路本地/保留段都排除了
    return bool(addr.is_global)


async def check_url_allowed(url: str) -> str:
    """校验 URL 并返回规范化结果；违规抛 `BlockedURLError`。

    只做**静态 + DNS 解析**层面的判断（见模块 docstring 的残留风险说明）。

    `CODEFORGE_WEB_ALLOW_PRIVATE=1` 可以放开私网/环回限制 —— 给"要抓内网文档站"
    这类明确需求用。默认关，因为默认开着就等于把内网暴露给一个会读网页内容的 agent。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(f"只支持 http/https，收到 {parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise BlockedURLError(f"URL 里没有主机名: {url}")

    if os.environ.get("CODEFORGE_WEB_ALLOW_PRIVATE") == "1":
        return url.strip()  # 显式放行：见 docstring

    # 字面 IP 直接判；域名走 DNS 解析
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as e:
        raise BlockedURLError(f"域名解析失败: {host}（{e}）") from e
    except OSError as e:
        raise BlockedURLError(f"域名解析失败: {host}（{e}）") from e

    ips = {info[4][0] for info in infos}
    if not ips:
        raise BlockedURLError(f"域名解析不到地址: {host}")
    blocked = sorted(ip for ip in ips if not _is_global_ip(ip))
    if blocked:
        raise BlockedURLError(
            f"拒绝访问非公网地址：{host} → {', '.join(blocked)}"
            "（环回/内网/链路本地/云元数据段都在此列；"
            "要访问本机服务请用 bash 的 curl，要放开私网请设 "
            "CODEFORGE_WEB_ALLOW_PRIVATE=1）"
        )
    return url.strip()


# ── 抓取 ──────────────────────────────────────────────────────────


async def fetch(
    url: str,
    *,
    timeout: float = WEB_TIMEOUT,
    max_bytes: int = WEB_MAX_BYTES,
    convert_html: bool = True,
) -> FetchResult:
    """抓一个 URL。

    重定向**手动逐跳**处理：`follow_redirects=True` 会让 httpx 自动跟到内网，
    而那样就绕过了守卫。

    `convert_html=False` 时返回**原始 HTML** —— 搜索后端要的是 HTML
    （`parse_search_results` 解析的是标签结构），转成纯文本就再也解析不出结果了。
    """
    current = await check_url_allowed(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        break
                    nxt = urljoin(current, location)
                    current = await check_url_allowed(nxt)  # 逐跳校验
                    continue

                body = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= max_bytes:
                        truncated = True
                        break

                raw = bytes(body[:max_bytes])
                content_type = resp.headers.get("content-type", "")
                return FetchResult(
                    status=resp.status_code,
                    url=current,
                    text=decode_body(raw, content_type, convert_html=convert_html),
                    content_type=content_type,
                    truncated=truncated,
                )
        raise BlockedURLError(f"重定向次数超过 {MAX_REDIRECTS}: {url}")


def decode_body(raw: bytes, content_type: str, *, convert_html: bool = True) -> str:
    """按 content-type 声明的 charset 解码；`convert_html` 时把 HTML 转成纯文本。"""
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    if m:
        charset = m.group(1)
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if convert_html and ("html" in content_type.lower() or text.lstrip()[:1] == "<"):
        return html_to_text(text)
    return text


# ── HTML → 文本 ───────────────────────────────────────────────────


#: 这些标签里的内容不是给人看的（脚本、样式、模板…），整体丢掉。
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})

#: 遇到这些标签补一个换行，免得所有文字挤成一行。
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "nav",
        "pre",
        "blockquote",
        "table",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "form",
        "hr",
    }
)


class _TextExtractor(HTMLParser):
    """把 HTML 抽成"人能读的文本"。刻意不做布局还原 —— 只要能读文档和报错。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # 折叠空白：行内多空格压成一个，连续空行压成一个
        lines = [" ".join(line.split()) for line in joined.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    """HTML → 纯文本（丢脚本/样式，按块级标签断行，折叠空白）。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 —— 畸形 HTML 不该让工具失败，返回已解析的部分
        pass
    return parser.text()


def page_title(html: str) -> str:
    """取 `<title>`。取不到返回空串。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001
        pass
    return parser.title


# ── 搜索结果解析 ──────────────────────────────────────────────────

#: 默认搜索后端。刻意选"无需 API key"的 DuckDuckGo HTML 版 —— 开箱能用。
#: 想换成带 key 的后端（Tavily/Brave/…）就设 `CODEFORGE_SEARCH_URL`，
#: 值是一个含 `{query}` 占位的 URL 模板。也可直接用 MCP 挂搜索 server。
DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"

#: 结果条数上限。
MAX_SEARCH_RESULTS = 20


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


def unwrap_redirect(url: str) -> str:
    """把搜索引擎的跳转壳剥掉。

    DuckDuckGo 的 html 版把真实地址放在 `?uddg=<percent-encoded>` 里；
    不剥的话返回给 agent 的是一串 `//duckduckgo.com/l/?uddg=...` 噪音。
    """
    if "uddg=" not in url:
        return url
    from urllib.parse import parse_qs, unquote

    query = urlparse(url).query or url.split("?", 1)[-1]
    values = parse_qs(query).get("uddg")
    if values and values[0]:
        return unquote(values[0])
    return url


class _SearchParser(HTMLParser):
    """抽 SERP 里的标题/链接/摘要。

    两档：
    * **结构化**（DuckDuckGo html 版）：认 `result__a` / `result__snippet` 类名；
    * **兜底**：认不出结构化标记时，把页面里所有指向 http(s) 的链接当结果，
      用链接文字当标题（覆盖 lite 版与多数同类页面）。

    刻意不用正则抠 HTML：类名与嵌套顺序都是站点实现细节，正则一改就崩；
    HTMLParser 至少能保证"结构变了也只是退化成兜底档"。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[SearchHit] = []
        self._href: str | None = None
        self._buf: list[str] = []
        self._mode: str | None = None  # "title" | "snippet" | "generic"
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag != "a" or self._skip_depth:
            return
        d = {k.lower(): (v or "") for k, v in attrs}
        href = d.get("href", "")
        cls = d.get("class", "")
        if "result__a" in cls:
            self._mode, self._href, self._buf = "title", unwrap_redirect(href), []
        elif "result__snippet" in cls:
            self._mode, self._href, self._buf = "snippet", None, []
        elif href.startswith(("http://", "https://")):
            self._mode, self._href, self._buf = "generic", unwrap_redirect(href), []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag != "a" or self._mode is None:
            return
        text = " ".join("".join(self._buf).split())
        if self._mode == "snippet":
            if self.hits and text:
                self.hits[-1].snippet = text
        elif self._href and text:
            self.hits.append(SearchHit(title=text, url=self._href))
        self._mode, self._href, self._buf = None, None, []

    def handle_data(self, data: str) -> None:
        if self._mode is not None and not self._skip_depth:
            self._buf.append(data)


def parse_search_results(html: str, limit: int = 5) -> list[SearchHit]:
    """把 SERP HTML 解析成结果列表（去重、截断到 limit）。"""
    parser = _SearchParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 —— 畸形页面返回已解析到的部分
        pass
    seen: set[str] = set()
    out: list[SearchHit] = []
    for hit in parser.hits:
        if not hit.url.startswith(("http://", "https://")) or hit.url in seen:
            continue
        seen.add(hit.url)
        out.append(hit)
        if len(out) >= limit:
            break
    return out


__all__ = [
    "DEFAULT_SEARCH_URL",
    "MAX_REDIRECTS",
    "MAX_SEARCH_RESULTS",
    "USER_AGENT",
    "WEB_MAX_BYTES",
    "WEB_TIMEOUT",
    "BlockedURLError",
    "FetchResult",
    "SearchHit",
    "check_url_allowed",
    "decode_body",
    "fetch",
    "html_to_text",
    "page_title",
    "parse_search_results",
    "unwrap_redirect",
]
