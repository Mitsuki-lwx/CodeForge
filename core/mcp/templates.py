"""MCP server 配置模板 —— 开箱可用、且**默认安全**的一组参数。

## 为什么需要模板

接一个 MCP server 的门槛不在"写 JSON"，而在**知道哪些参数是安全默认**。
以 `@playwright/mcp` 为例，它的参数里同时有：

* `--headless` / `--isolated` / `--output-dir` —— 该给的（无人值守、不落 profile、产物归位）
* `--image-responses omit` —— 文本模型看不了图，`omit` 省 token 也避免"模型假装看过"
* `--browser msedge` —— 用系统已有的 Edge，**省掉 178MB 的 Chromium 下载**
* `--allow-unrestricted-file-access` / `--no-sandbox` —— **绝不能默认给**（放开文件访问 / 关沙箱）

让用户自己去翻 `--help` 拼这些，结果多半是"抄了别人的配置，顺手带上 `--no-sandbox`"。

模板把这些决定**写死成代码**，并给每个参数注明理由。
"""

from __future__ import annotations

from typing import Any

#: 模板里的 server 名（也是工具前缀：`playwright__browser_click`）
PLAYWRIGHT_SERVER_NAME = "playwright"

#: 模板默认用的浏览器 channel。Windows 自带 Edge；换 `chrome` 也行。
#: 用系统浏览器而不是下 178MB 的 Chromium —— 实测那个 CDN 在某些网络下只有
#: 0.15MB/s（要 20 分钟），而 Edge 是现成的。
DEFAULT_BROWSER = "msedge"

#: 系统浏览器的候选安装路径（按平台）。
#:
#: ⚠️ 为什么优先用 `--executable-path` 而不是 `--browser <channel>`：
#: 实测 Node 版 playwright 的 channel 表**只认** `C:\Program Files\Microsoft\Edge\...`，
#: 而这台机器上 Edge 装在 `Program Files (x86)\...` —— 于是 `--browser msedge`
#: 报 `Chromium distribution 'msedge' is not found`，而指向真实路径的
#: `--executable-path` 直接成功。探测真实路径比信任 channel 表可靠。
_BROWSER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "win32": (
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ),
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ),
}

#: Linux 上按 PATH 找这些可执行名。
_LINUX_BROWSER_BINARIES = (
    "microsoft-edge",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def find_system_browser() -> str | None:
    """找一个可用的系统浏览器可执行文件；找不到返回 None。

    找不到也不是错：那样就不传浏览器参数，让 MCP server 用它自带的 Chromium
    （代价是要先 `npx playwright install chromium`，约 178MB）。
    """
    import os
    import shutil
    import sys

    if sys.platform == "win32":
        for raw in _BROWSER_CANDIDATES["win32"]:
            path = os.path.expandvars(raw)
            if os.path.isfile(path):
                return path
        return None
    if sys.platform == "darwin":
        for path in _BROWSER_CANDIDATES["darwin"]:
            if os.path.isfile(path):
                return path
        return None
    for name in _LINUX_BROWSER_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


#: 截图等自动命名产物的落盘目录（相对工作区）。
DEFAULT_OUTPUT_DIR = ".codeforge/screenshots"

#: 单次工具调用超时（毫秒）。浏览器冷启动 + 导航会超过默认的 60s。
DEFAULT_TIMEOUT_MS = 120_000


def playwright_server_config(
    *,
    browser: str = "",
    executable_path: str | None = None,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    allow_images: bool = False,
    allowed_origins: str = "",
    blocked_origins: str = "",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """`@playwright/mcp` 的 stdio server 配置（安全默认）。

    浏览器怎么选（按优先级）：
    1. `executable_path` 显式给 → `--executable-path`；
    2. `browser` 显式给 → `--browser <channel>`；
    3. 都不给 → **自动探测**系统浏览器路径 → `--executable-path`；
    4. 探测不到 → 不传浏览器参数，让 server 用自带 Chromium
       （要先 `npx playwright install chromium`）。

    为什么默认走"探测路径"而不是 `--browser msedge`：见 `_BROWSER_CANDIDATES` 的注释
    —— Node 版 playwright 的 channel 表只认 x64 路径，而 Edge 常常装在 x86 路径下，
    于是 channel 方式会直接报 `Chromium distribution 'msedge' is not found`。

    Args:
        browser: 浏览器 channel（`msedge` / `chrome` / `firefox` / `webkit`）。
        executable_path: 浏览器可执行文件的完整路径。
        output_dir: 自动命名产物（截图等）的目录。
        allow_images: 是否让 server 把图像回给模型。
            **默认 False**（`--image-responses omit`）—— 文本模型看不了图，
            传了只是烧 token，还可能让模型以为自己看过。多模态模型才设 True。
        allowed_origins: 只允许浏览器请求这些 origin（分号分隔）。
            留空 = 不限制。注意 server 自己文档里写明它**不是安全边界**
            （不挡重定向），所以它是"少出网"的收敛手段，不是沙箱。
        blocked_origins: 屏蔽的 origin（分号分隔），优先于 allowlist。
        timeout_ms: 单次工具调用超时。
    """
    args: list[str] = [
        "-y",
        "@playwright/mcp@latest",
        "--headless",  # 无人值守：agent 环境没有显示器
        "--isolated",  # profile 留在内存，不落盘（不污染用户浏览器配置）
        "--output-dir",
        output_dir,
        # 文本模型看不了图 —— 见 allow_images 说明
        "--image-responses",
        "allow" if allow_images else "omit",
    ]

    resolved_exe = executable_path
    if resolved_exe is None and not browser:
        resolved_exe = find_system_browser()
    if resolved_exe:
        args += ["--executable-path", resolved_exe]
    elif browser:
        args += ["--browser", browser]

    if allowed_origins:
        args += ["--allowed-origins", allowed_origins]
    if blocked_origins:
        args += ["--blocked-origins", blocked_origins]

    return {
        "type": "stdio",
        "command": "npx",
        "args": args,
        "timeout": timeout_ms,
    }


#: 模板登记表：`codeforge mcp add <name>` 按名字取。
TEMPLATES: dict[str, Any] = {
    PLAYWRIGHT_SERVER_NAME: playwright_server_config,
}


def describe_flags(resolved: dict[str, Any] | None = None) -> list[str]:
    """给 CLI/文档用：说明模板里每个参数为什么在那儿。"""
    lines = [
        "--headless          无人值守（agent 环境没有显示器）",
        "--isolated          profile 留内存，不污染你的浏览器配置",
        f"--output-dir        截图等产物落到 {DEFAULT_OUTPUT_DIR}",
        "--image-responses   文本模型用 omit（省 token，也不假装看过图）",
    ]
    if resolved:
        args = resolved.get("args") or []
        if "--executable-path" in args:
            exe = args[args.index("--executable-path") + 1]
            lines.append(
                f"--executable-path   复用系统已有浏览器，省掉 178MB 下载：{exe}"
            )
        elif "--browser" in args:
            ch = args[args.index("--browser") + 1]
            lines.append(f"--browser           用 channel {ch}")
        else:
            lines.append(
                "（没找到系统浏览器 → 用 server 自带的 Chromium；"
                "先跑 npx playwright install chromium）"
            )
    lines.append(
        "（刻意**不**给 --no-sandbox / --allow-unrestricted-file-access：那会放开沙箱与文件访问）"
    )
    return lines


__all__ = [
    "DEFAULT_BROWSER",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_TIMEOUT_MS",
    "PLAYWRIGHT_SERVER_NAME",
    "TEMPLATES",
    "describe_flags",
    "find_system_browser",
    "playwright_server_config",
]
