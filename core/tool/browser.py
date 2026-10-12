"""浏览器验证 —— 用真实 Chromium 打开页面，看它**真的**渲染成什么样。

## 为什么需要它

后端逻辑可以用 `bash` 跑 pytest 验证；但"这个页面渲染对了吗"没法靠读代码回答。
没有浏览器，agent 写完前端只能说"看起来对"。这个模块给它的是一台真的浏览器：
打开 URL、等它加载、把可见文本、控制台报错、页面异常读回来。

## 刻意做成**无状态**（每次调用起一个浏览器，用完就关）

浏览器是有生命周期的对象（进程、上下文、页面），而会话结束时的清理是**同步**的
（`SessionBundle.close()`），异步的 playwright 关不干净。持久化一个浏览器要么
引入后台线程 + 任务队列，要么在退出时漏进程 —— 两条都比"每次起一个"更容易出错。

代价是每次调用多 ~1s 的启动开销。对"验证页面"这个用途是划算的：
无状态 = 无泄漏、无竞态、无清理逻辑要维护。

需要"点击后再看"的场景用 `js` 参数表达（`browser_snapshot(url, js="...")`），
JS 里点完再读 DOM 即可。

## 权限：`command` 档，要审批

浏览器能访问**内网**（这正是它能验证你本机 dev server 的原因），也能被页面里的
内容牵着去发请求。所以它和 `web_fetch` 一样默认要过审批；想免审批就加一条规则：

    {"permissions": {"rules": [{"tool": "browser_snapshot", "pattern": "*", "action": "allow"}]}}

⚠️ 在有敏感内网的环境里，不要给浏览器工具免审批。

## 依赖是可选的

`playwright` 不在必装依赖里（它要下 ~150MB 的 Chromium）。没装时工具会返回一句
可操作的错误，而不是 ImportError 崩掉：

    pip install "codeforge[browser]" && playwright install chromium
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.tool.context import ExecutionContext

#: 截图默认落盘目录（相对工作区）。刻意**不给调用方任意路径** ——
#: 那等于给了一条绕过路径沙箱的写文件通道。
SCREENSHOT_DIR = Path(".codeforge") / "screenshots"

#: 单次页面操作的总超时（秒）。Chromium 冷启动本身就要 ~1s。
BROWSER_TIMEOUT = 60.0

#: 页面导航超时（毫秒）。
NAV_TIMEOUT_MS = 20_000

#: 返回给模型的可见文本上限。
MAX_SNAPSHOT_CHARS = 20_000

#: 控制台/异常最多记多少条（页面可能刷几千条日志）。
MAX_CONSOLE_ENTRIES = 50

INSTALL_HINT = (
    "需要可选依赖 playwright：\n"
    '  pip install "codeforge[browser]"\n'
    "然后二选一：\n"
    "  playwright install chromium          # 下 ~178MB 自带内核（最可复现）\n"
    "  或设 CODEFORGE_BROWSER_CHANNEL=msedge  # 用系统已有的 Edge/Chrome，免下载"
)

#: 启动浏览器时**依次尝试**的 channel。空串 = playwright 自带的 Chromium。
#:
#: 顺序刻意如此：自带内核最可复现 → 没有就退到系统已有的 Edge / Chrome。
#: Windows 自带 Edge，于是"验证 UI"这件事**不需要先下 178MB**（实测该 CDN
#: 在本机只有 0.15MB/s，要 20 分钟；而 Edge 是现成的）。
#: 想固定用哪个，设 `CODEFORGE_BROWSER_CHANNEL`。
_CHANNELS: tuple[str, ...] = ("", "msedge", "chrome")


class BrowserUnavailableError(RuntimeError):
    """没装 playwright / 没装浏览器内核。"""


@dataclass
class PageSnapshot:
    """一次页面访问的可观测结果。"""

    url: str
    title: str
    text: str
    console: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: int | None = None
    screenshot: Path | None = None

    def report(self, *, max_chars: int = MAX_SNAPSHOT_CHARS) -> str:
        """渲染成给模型看的文本。"""
        lines = [f"{self.status if self.status is not None else '?'} {self.url}"]
        if self.title:
            lines.append(f"title: {self.title}")
        if self.errors:
            lines.append("\n⚠️ 页面异常：")
            lines.extend(f"  - {e}" for e in self.errors)
        if self.console:
            lines.append("\n控制台输出：")
            lines.extend(f"  {c}" for c in self.console)
        text = self.text
        if len(text) > max_chars:
            text = (
                text[:max_chars]
                + f"\n…（还有 {len(self.text) - max_chars} 字符未显示）"
            )
        lines.append("\n可见文本：\n" + (text or "(空)"))
        if self.screenshot is not None:
            lines.append(f"\n截图已保存：{self.screenshot}")
        return "\n".join(lines)


def _require_playwright():
    """延迟 import；没装就给一句能照做的错误。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover —— 取决于环境
        raise BrowserUnavailableError(INSTALL_HINT) from e
    return async_playwright


async def _launch(pw: Any):
    """按 `_CHANNELS` 依次尝试启动，第一个成功的就用它。

    为什么不是"直接 launch()"：那样没下自带内核就直接失败，而用户机器上
    通常已经有 Edge/Chrome —— 白等 178MB 下载毫无必要。
    """
    import os

    override = os.environ.get("CODEFORGE_BROWSER_CHANNEL")
    channels = (override,) if override is not None else _CHANNELS
    last: Exception | None = None
    for channel in channels:
        kwargs: dict[str, Any] = {"headless": True}
        if channel:
            kwargs["channel"] = channel
        try:
            return await pw.chromium.launch(**kwargs)
        except Exception as e:  # noqa: BLE001 —— 逐个回退，最后统一报
            last = e
    raise BrowserUnavailableError(
        f"没有可用的浏览器内核（试过：{', '.join(c or '自带 Chromium' for c in channels)}）。\n"
        f"最后一个错误：{type(last).__name__}: {last}\n{INSTALL_HINT}"
    )


async def visit(
    url: str,
    *,
    js: str = "",
    screenshot_to: Path | None = None,
    full_page: bool = False,
    timeout_ms: int = NAV_TIMEOUT_MS,
    eval_only: bool = False,
) -> PageSnapshot:
    """打开 URL，可选跑一段 JS，把页面状态读回来。

    Args:
        url: 要打开的地址（**允许 localhost/内网** —— 这就是它的用途）。
        js: 加载后执行的 JS（表达式或语句）；结果会放进返回值。
        screenshot_to: 需要截图时给目标路径。
        full_page: 截整页还是只截视口。
        timeout_ms: 导航超时。
        eval_only: 只回 JS 结果，不回整页文本（`browser_eval` 用）。
    """
    async_playwright = _require_playwright()
    console: list[str] = []
    errors: list[str] = []
    status: int | None = None

    async with async_playwright() as pw:
        browser = await _launch(pw)

        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()

            def _on_console(msg: Any) -> None:
                if len(console) < MAX_CONSOLE_ENTRIES:
                    console.append(f"[{msg.type}] {msg.text}")

            def _on_pageerror(err: Any) -> None:
                if len(errors) < MAX_CONSOLE_ENTRIES:
                    errors.append(str(err))

            def _on_requestfailed(req: Any) -> None:
                if len(errors) < MAX_CONSOLE_ENTRIES:
                    errors.append(f"请求失败 {req.url}")

            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)
            page.on("requestfailed", _on_requestfailed)

            resp = await page.goto(url, wait_until="load", timeout=timeout_ms)
            if resp is not None:
                status = resp.status

            if js:
                result = await page.evaluate(js)
                if eval_only:
                    return PageSnapshot(
                        url=page.url,
                        title=await page.title(),
                        text=_render_js_result(result),
                        status=status,
                    )

            title = await page.title()
            text = await page.evaluate("document.body ? document.body.innerText : ''")

            shot: Path | None = None
            if screenshot_to is not None:
                screenshot_to.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot_to), full_page=full_page)
                shot = screenshot_to

            return PageSnapshot(
                url=page.url,
                title=title or "",
                text=text or "",
                console=console,
                errors=errors,
                status=status,
                screenshot=shot,
            )
        finally:
            await browser.close()


@dataclass
class DiffResult:
    """一次「交互前 / 交互后」的截图对比结果。"""

    url: str
    total_pixels: int
    changed_pixels: int
    before_png: bytes
    after_png: bytes
    diff_png: bytes | None = None

    @property
    def ratio(self) -> float:
        """变化像素占比（0.0 ~ 1.0）。"""
        if self.total_pixels <= 0:
            return 0.0
        return self.changed_pixels / self.total_pixels

    def report(
        self,
        *,
        before_path: Path | None = None,
        after_path: Path | None = None,
        diff_path: Path | None = None,
    ) -> str:
        pct = self.ratio * 100
        verdict = (
            "界面**没有可见变化**"
            if self.changed_pixels == 0
            else f"界面有可见变化（{pct:.2f}% 的像素不同）"
        )
        lines = [
            f"{self.url}",
            verdict,
            f"变化像素: {self.changed_pixels} / {self.total_pixels}（{pct:.2f}%）",
        ]
        for label, p in (
            ("交互前", before_path),
            ("交互后", after_path),
            ("差异图", diff_path),
        ):
            if p is not None:
                lines.append(f"{label}截图: {p}")
        return "\n".join(lines)


#: 像素比对时每通道允许的差值。抗锯齿/亚像素渲染会让"看起来一样"的两张图
#: 有 ±1~5 的抖动 —— 不容忍它就会把噪声报成"变了"。
DIFF_CHANNEL_TOLERANCE = 8

#: 在页面里做像素比对的 JS。
#:
#: 刻意**不引入 Pillow**：浏览器本来就有 PNG 解码器和 canvas，
#: 让它在页面里算，零新依赖、也不用把几十 MB 的图像数据搬进 Python。
#: 差异图直接在 canvas 上画出来（变化像素涂红），再导出 dataURL。
_DIFF_JS = """
async ([beforeB64, afterB64, tolerance]) => {
  const load = (b64) => new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => rej(new Error('image decode failed'));
    img.src = 'data:image/png;base64,' + b64;
  });
  const [a, b] = await Promise.all([load(beforeB64), load(afterB64)]);
  const w = Math.min(a.width, b.width), h = Math.min(a.height, b.height);
  const read = (img) => {
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const x = c.getContext('2d');
    x.drawImage(img, 0, 0);
    return x.getImageData(0, 0, w, h).data;
  };
  const da = read(a), db = read(b);
  const out = document.createElement('canvas');
  out.width = w; out.height = h;
  const octx = out.getContext('2d');
  const odata = octx.createImageData(w, h);
  let changed = 0;
  for (let i = 0; i < da.length; i += 4) {
    const diff = Math.max(
      Math.abs(da[i] - db[i]),
      Math.abs(da[i+1] - db[i+1]),
      Math.abs(da[i+2] - db[i+2])
    );
    if (diff > tolerance) {
      changed++;
      odata.data[i] = 255; odata.data[i+1] = 0; odata.data[i+2] = 0;
      odata.data[i+3] = 255;
    } else {
      // 没变的像素画成淡灰，方便一眼看出"哪一块动了"
      odata.data[i] = 235; odata.data[i+1] = 235; odata.data[i+2] = 235;
      odata.data[i+3] = 255;
    }
  }
  octx.putImageData(odata, 0, 0);
  return {total: w * h, changed, diff: out.toDataURL('image/png').split(',')[1]};
}
"""


async def visit_with_diff(
    url: str,
    *,
    js: str = "",
    full_page: bool = False,
    tolerance: int = DIFF_CHANNEL_TOLERANCE,
    timeout_ms: int = NAV_TIMEOUT_MS,
) -> DiffResult:
    """打开 URL → 截一张 → 跑 `js`（交互）→ 再截一张 → 比对像素。

    这是"文本断言答不了"的那类验证：点击按钮之后界面**真的**变了吗？
    `changed == 0` 就是"交互没有任何可见效果" —— 一个很常见、也很容易被
    "日志里有调用"掩盖的 bug。
    """
    import base64

    async_playwright = _require_playwright()
    async with async_playwright() as pw:
        browser = await _launch(pw)
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            page = await context.new_page()
            await page.goto(url, wait_until="load", timeout=timeout_ms)

            before_png = await page.screenshot(full_page=full_page)
            if js:
                await page.evaluate(js)
            after_png = await page.screenshot(full_page=full_page)

            stats = await page.evaluate(
                _DIFF_JS,
                [
                    base64.b64encode(before_png).decode("ascii"),
                    base64.b64encode(after_png).decode("ascii"),
                    int(tolerance),
                ],
            )
            diff_png = base64.b64decode(stats["diff"]) if stats.get("diff") else None
            return DiffResult(
                url=page.url,
                total_pixels=int(stats.get("total", 0)),
                changed_pixels=int(stats.get("changed", 0)),
                before_png=before_png,
                after_png=after_png,
                diff_png=diff_png,
            )
        finally:
            await browser.close()


def _render_js_result(result: Any) -> str:
    """把 JS 返回值渲染成文本。"""
    import json

    if result is None:
        return "(JS 返回 null)"
    if isinstance(result, (str, int, float, bool)):
        return str(result)
    try:
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return repr(result)


def screenshot_path(context: ExecutionContext, label: str = "") -> Path:
    """给截图选一个落盘路径（工作区内的 `.codeforge/screenshots/`）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in label if c.isalnum() or c in "-_")[:40] or "page"
    return Path(context.cwd) / SCREENSHOT_DIR / f"{stamp}-{safe}.png"


__all__ = [
    "BROWSER_TIMEOUT",
    "DIFF_CHANNEL_TOLERANCE",
    "INSTALL_HINT",
    "MAX_SNAPSHOT_CHARS",
    "SCREENSHOT_DIR",
    "BrowserUnavailableError",
    "DiffResult",
    "PageSnapshot",
    "screenshot_path",
    "visit",
    "visit_with_diff",
]
