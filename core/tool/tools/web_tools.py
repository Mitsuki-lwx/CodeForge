"""联网工具 —— `web_fetch` 与 `web_search`。

这两个工具补的是 agent 的**外部世界验证能力**：没有它们，遇到没见过的报错
只能猜，写完前端只能"看起来对"（起服务由后台 shell 负责，见 `core/tool/shells.py`）。

## 权限档位：两者都是 `command`，**刻意不自动放行**

它们读的是外部世界，不是本地文件。自动放行的代价是：
* **外泄**：URL 里可以塞数据（`?q=<把读到的东西拼进去>`）；
* **注入**：抓回来的页面内容可能带指令，直接进上下文。

所以它们和 `bash` 同级：交互模式下要审批、无人值守下由策略代答、可被规则引擎
放行或拒绝。想让它免审批（比如只读文档），在 `.codeforge/settings.json` 里加：

    {"permissions": {"rules": [
        {"tool": "web_fetch",  "pattern": "*", "action": "allow"},
        {"tool": "web_search", "pattern": "*", "action": "allow"}
    ]}}

这是一次**显式、可审计**的授权，比默认全开好。
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus

from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult
from core.tool.web import (
    DEFAULT_SEARCH_URL,
    MAX_SEARCH_RESULTS,
    BlockedURLError,
    fetch,
    parse_search_results,
)

#: `web_fetch` 单次返回给模型的字符上限（页面全文往往几十万字符，灌不进上下文）。
MAX_RETURN_CHARS = 20_000


class WebFetchTool(Tool):
    """抓一个 URL 并转成可读文本。"""

    timeout_seconds = 40.0
    max_retries = 0

    def name(self) -> str:
        return "web_fetch"

    def description(self) -> str:
        return (
            "Fetch a URL and return it as readable text (HTML is converted, "
            "scripts/styles dropped). Use it to read documentation, changelogs, "
            "or an error message you do not recognise. Only public http/https "
            "addresses are reachable — loopback, private and cloud-metadata "
            "ranges are refused, so to hit a service you started locally use "
            "bash with curl instead."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整 URL（含 http/https）"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 500,
                    "description": "只返回前这么多字符（默认 20000）",
                },
            },
            "required": ["url"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        url = str(input["url"])
        limit = int(input.get("max_chars") or MAX_RETURN_CHARS)
        try:
            res = await fetch(url)
        except BlockedURLError as e:
            return ToolResult(
                success=False,
                error=f"URL 被拒绝: {e}",
                meta={"url": url, "blocked": True},
            )
        except Exception as e:  # noqa: BLE001 —— 网络问题要报清楚，不抛
            return ToolResult(
                success=False,
                error=f"抓取失败: {type(e).__name__}: {e}",
                meta={"url": url},
            )

        text = res.text
        note = ""
        if len(text) > limit:
            note = f"\n…（还有 {len(text) - limit} 字符未显示，可加大 max_chars）"
            text = text[:limit]
        if res.truncated:
            note += "\n（响应体超过 2MB 上限，已截断）"

        ok = 200 <= res.status < 300
        return ToolResult(
            success=ok,
            data=(
                f"GET {res.url} → {res.status} ({res.content_type or 'unknown'})\n"
                f"---\n{text or '(无文本内容)'}{note}"
            ),
            error=None if ok else f"HTTP {res.status}",
            meta={"url": res.url, "status": res.status},
        )

    def is_read_only(self) -> bool:
        # 语义上只读（不改本地状态），但**分类是 command** → 仍要过审批。
        # 见模块 docstring：出站请求不能被当成"读本地文件"那样自动放行。
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"


class WebSearchTool(Tool):
    """搜网页，返回标题/链接/摘要。"""

    timeout_seconds = 40.0
    max_retries = 0

    def name(self) -> str:
        return "web_search"

    def description(self) -> str:
        return (
            "Search the web and return the top results (title, url, snippet). "
            "Use it to find the right documentation page or to check whether a "
            "library version / API exists, then web_fetch the promising link. "
            "Backend defaults to DuckDuckGo HTML; override with the "
            "CODEFORGE_SEARCH_URL env var (a URL template containing {query})."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_RESULTS,
                    "description": f"返回条数（默认 5，上限 {MAX_SEARCH_RESULTS}）",
                },
            },
            "required": ["query"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        query = str(input["query"]).strip()
        if not query:
            return ToolResult(success=False, error="query 不能为空")
        limit = int(input.get("max_results") or 5)

        template = os.environ.get("CODEFORGE_SEARCH_URL") or DEFAULT_SEARCH_URL
        if "{query}" not in template:
            return ToolResult(
                success=False,
                error="CODEFORGE_SEARCH_URL 必须是含 {query} 占位的 URL 模板",
            )
        url = template.replace("{query}", quote_plus(query))

        try:
            # 要原始 HTML：搜索结果的解析靠标签结构，转成纯文本就解析不出来了
            res = await fetch(url, convert_html=False)
        except BlockedURLError as e:
            return ToolResult(
                success=False, error=f"搜索后端被拒绝: {e}", meta={"query": query}
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=(
                    f"搜索失败: {type(e).__name__}: {e}\n"
                    "（默认后端是 DuckDuckGo HTML，部分网络环境不可达；"
                    "可用 CODEFORGE_SEARCH_URL 换后端，或改用 MCP 搜索 server）"
                ),
                meta={"query": query},
            )

        if res.status >= 400:
            return ToolResult(
                success=False,
                error=f"搜索后端返回 HTTP {res.status}",
                meta={"query": query, "status": res.status},
            )

        hits = parse_search_results(res.text, limit)
        if not hits:
            return ToolResult(
                success=True,
                data=f"没有解析到结果（query={query}，HTTP {res.status}）。页面结构可能变了。",
                meta={"query": query, "count": 0},
            )

        lines = [f"{i}. {h.title}\n   {h.url}" for i, h in enumerate(hits, 1)]
        for i, h in enumerate(hits):
            if h.snippet:
                lines[i] += f"\n   {h.snippet}"
        return ToolResult(
            success=True,
            data=f"query: {query}\n" + "\n".join(lines),
            meta={"query": query, "count": len(hits)},
        )

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"


__all__ = ["WebFetchTool", "WebSearchTool"]
