"""浏览器工具 —— 用真实 Chromium 验证页面**真的**渲染成什么样。

三个工具，都是无状态的（每次调用起一个浏览器，用完就关，见
`core/tool/browser.py` 的设计说明）：

* `browser_snapshot` —— 打开 URL，回标题 / 可见文本 / 控制台 / 页面异常。
  这是主力：控制台报错和未捕获异常是"页面坏了"最直接的证据。
* `browser_eval` —— 打开 URL 跑一段 JS，回结果。用来做**断言**
  （`document.querySelector('#app') !== null`）。
* `browser_screenshot` —— 打开 URL 存一张 PNG，回路径（给人看；模型看不到图）。

三者都 `category="command"`：浏览器能访问内网（这正是它能验证本机服务的原因），
也挡不住页面内容牵着它发请求 —— 所以默认要审批，见 `core/tool/browser.py`。
"""

from __future__ import annotations

from core.tool.browser import (
    BROWSER_TIMEOUT,
    MAX_SNAPSHOT_CHARS,
    BrowserUnavailableError,
    screenshot_path,
    visit,
    visit_with_diff,
)
from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class _BrowserTool(Tool):
    """三个浏览器工具的公共部分。"""

    timeout_seconds = BROWSER_TIMEOUT
    max_retries = 0  # 起浏览器不便宜，失败就报错，别自动重来

    def is_read_only(self) -> bool:
        # 语义上不改本地状态；但分类是 command → 仍要过审批（见模块 docstring）
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "command"

    @staticmethod
    def _url_schema() -> dict:
        return {
            "type": "string",
            "description": "要打开的完整 URL（http/https；localhost 也可以）",
        }

    @staticmethod
    def _js_schema() -> dict:
        return {
            "type": "string",
            "description": (
                "加载后执行的 JS。表达式或语句都行；"
                "用它点击（document.querySelector(...).click()）或断言。"
            ),
        }


class BrowserSnapshotTool(_BrowserTool):
    """打开页面并读回它的可见文本、控制台与异常。"""

    def name(self) -> str:
        return "browser_snapshot"

    def description(self) -> str:
        return (
            "Open a URL in a real headless Chromium and report what actually "
            "rendered: page title, visible text, console output and page errors. "
            "Use this to verify a UI you just built instead of guessing. Pass "
            "`js` to interact first (e.g. click a button) and then read the result. "
            "localhost is allowed — that is the point of this tool."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": self._url_schema(),
                "js": self._js_schema(),
                "max_chars": {
                    "type": "integer",
                    "minimum": 200,
                    "description": f"返回的可见文本上限（默认 {MAX_SNAPSHOT_CHARS}）",
                },
            },
            "required": ["url"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        url = str(input["url"])
        js = str(input.get("js") or "")
        limit = int(input.get("max_chars") or MAX_SNAPSHOT_CHARS)
        try:
            snap = await visit(url, js=js)
        except BrowserUnavailableError as e:
            return ToolResult(success=False, error=str(e), meta={"url": url})
        except Exception as e:  # noqa: BLE001 —— 导航失败要报清楚
            return ToolResult(
                success=False,
                error=f"打开页面失败: {type(e).__name__}: {e}",
                meta={"url": url},
            )
        return ToolResult(
            success=True,
            data=snap.report(max_chars=limit),
            meta={
                "url": snap.url,
                "status": snap.status,
                "title": snap.title,
                "errors": len(snap.errors),
            },
        )


class BrowserEvalTool(_BrowserTool):
    """打开页面跑一段 JS，返回结果 —— 用来做断言。"""

    def name(self) -> str:
        return "browser_eval"

    def description(self) -> str:
        return (
            "Open a URL, run a JavaScript expression and return its value "
            "(JSON-encoded for objects/arrays). Use it for assertions, e.g. "
            "`document.querySelectorAll('.todo-item').length` or "
            "`document.title`. Return value is the raw JS result, not the page text."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": self._url_schema(),
                "js": {
                    "type": "string",
                    "description": "要执行的 JS 表达式（返回值会被回传）",
                },
            },
            "required": ["url", "js"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        url = str(input["url"])
        js = str(input["js"])
        try:
            snap = await visit(url, js=js, eval_only=True)
        except BrowserUnavailableError as e:
            return ToolResult(success=False, error=str(e), meta={"url": url})
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"执行 JS 失败: {type(e).__name__}: {e}",
                meta={"url": url, "js": js},
            )
        return ToolResult(
            success=True,
            data=f"{snap.url}\nJS 结果: {snap.text}",
            meta={"url": snap.url, "status": snap.status},
        )


class BrowserScreenshotTool(_BrowserTool):
    """打开页面存一张 PNG。"""

    def name(self) -> str:
        return "browser_screenshot"

    def description(self) -> str:
        return (
            "Open a URL in headless Chromium and save a PNG screenshot, returning "
            "the file path so a human can look at it. Note the model itself cannot "
            "see the image — pair it with browser_snapshot/browser_eval for checks "
            "you can actually assert on. Pass `js` to interact before shooting."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": self._url_schema(),
                "js": self._js_schema(),
                "label": {
                    "type": "string",
                    "description": "文件名里的可读标签（如 login-page）",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "截整页（默认只截视口）",
                },
            },
            "required": ["url"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        url = str(input["url"])
        js = str(input.get("js") or "")
        label = str(input.get("label") or "")
        # 刻意**不接受任意输出路径** —— 那等于开一条绕过路径沙箱的写文件通道
        target = screenshot_path(context, label or url.rsplit("/", 1)[-1] or "page")
        try:
            snap = await visit(
                url,
                js=js,
                screenshot_to=target,
                full_page=bool(input.get("full_page")),
            )
        except BrowserUnavailableError as e:
            return ToolResult(success=False, error=str(e), meta={"url": url})
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"截图失败: {type(e).__name__}: {e}",
                meta={"url": url},
            )
        return ToolResult(
            success=True,
            data=(
                f"截图已保存: {snap.screenshot}\n"
                f"{snap.status} {snap.url}"
                + (f"\ntitle: {snap.title}" if snap.title else "")
                + (
                    f"\n⚠️ 页面异常 {len(snap.errors)} 条（用 browser_snapshot 看详情）"
                    if snap.errors
                    else ""
                )
            ),
            meta={
                "url": snap.url,
                "status": snap.status,
                "screenshot": str(snap.screenshot),
            },
            # 图像附件：**能不能被模型看到**由 provider 的 `vision` 决定
            # （见 `ToolExecutionMixin._vision_enabled`）。不支持时执行层会丢掉它，
            # 并在文本里说明"图还在磁盘上"。
            images=_read_png(snap.screenshot) if snap.screenshot else [],
        )


class BrowserDiffTool(_BrowserTool):
    """截一张 → 交互 → 再截一张 → 比对像素：界面**真的**变了吗。"""

    def name(self) -> str:
        return "browser_diff"

    def description(self) -> str:
        return (
            "Open a URL, screenshot it, run `js` (e.g. click a button), screenshot "
            "again, and report how many pixels changed. This answers what text "
            "assertions cannot: did the interaction have any *visible* effect? "
            "0 changed pixels means the UI did nothing — a common bug that logs and "
            "DOM checks happily hide. Saves before/after/diff PNGs for a human."
        )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": self._url_schema(),
                "js": {
                    "type": "string",
                    "description": "两次截图之间执行的 JS（通常是点击）",
                },
                "label": {
                    "type": "string",
                    "description": "文件名里的可读标签（如 after-click）",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "截整页（默认只截视口）",
                },
            },
            "required": ["url"],
        }

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        url = str(input["url"])
        js = str(input.get("js") or "")
        label = str(input.get("label") or "diff")
        try:
            diff = await visit_with_diff(
                url, js=js, full_page=bool(input.get("full_page"))
            )
        except BrowserUnavailableError as e:
            return ToolResult(success=False, error=str(e), meta={"url": url})
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"截图对比失败: {type(e).__name__}: {e}",
                meta={"url": url},
            )

        before_path = screenshot_path(context, f"{label}-before")
        after_path = screenshot_path(context, f"{label}-after")
        diff_path = screenshot_path(context, f"{label}-diff")
        for path, blob in (
            (before_path, diff.before_png),
            (after_path, diff.after_png),
            (diff_path, diff.diff_png),
        ):
            if blob is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)

        return ToolResult(
            success=True,
            data=diff.report(
                before_path=before_path,
                after_path=after_path,
                diff_path=diff_path if diff.diff_png else None,
            ),
            meta={
                "url": diff.url,
                "changed_pixels": diff.changed_pixels,
                "total_pixels": diff.total_pixels,
                "ratio": round(diff.ratio, 6),
            },
            images=_read_png(after_path) if after_path.exists() else [],
        )


def _read_png(path) -> list[dict]:
    """把 PNG 读成图像附件（base64）。

    读失败就返回空 —— 截图已经落盘了，附件只是"能不能被模型看到"的加分项，
    不该因为它让整个工具调用失败。
    """
    import base64
    from pathlib import Path

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return []
    return [{"media_type": "image/png", "data": base64.b64encode(raw).decode("ascii")}]


__all__ = [
    "BrowserDiffTool",
    "BrowserEvalTool",
    "BrowserScreenshotTool",
    "BrowserSnapshotTool",
]
