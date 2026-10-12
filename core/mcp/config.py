"""MCP 配置加载器 —— 解析 `~/.codeforge/mcp.json` 与工作区的 `.codeforge/mcp.json`。

## ⚠️ 项目级配置默认**不加载**（安全决定）

MCP 的 stdio server 就是"启动一个子进程"。工作区里的 `.codeforge/mcp.json` 是
**仓库带进来的文件**，如果默认就加载，那么：

    git clone <任意仓库> && cd <仓库> && codeforge

会在**会话启动时**、用户还没输入任何东西之前，执行仓库指定的命令 ——
而且子进程默认继承完整环境变量（含各家 API key）。
这条路径**不需要 agent 参与**，也没有审批环节可绕过，是典型的"打开仓库即被 RCE"。

所以默认只加载**用户级** `~/.codeforge/mcp.json`（用户自己写的 = 用户自己的授权）。
要用项目自带的，显式打开 `features.mcp.trust_project`（或设
`CODEFORGE_TRUST_PROJECT_MCP=1`）—— 那是一次**明确、可审计**的授权。

调用方显式传 `config_path` 时视为已授权（调用方自己知道在加载什么），
这条保留给测试与显式指定的场景。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 用户级配置（永远信任：那是用户自己写的）
USER_CONFIG_PATH = Path.home() / ".codeforge" / "mcp.json"

#: 项目级配置（**默认不加载**，见模块 docstring）
PROJECT_CONFIG_PATH = Path(".codeforge") / "mcp.json"

#: 环境变量逃生口：显式信任项目级配置
TRUST_ENV_VAR = "CODEFORGE_TRUST_PROJECT_MCP"

#: 传给 MCP 子进程的**最小环境变量集**。
#:
#: 刻意不透传 `os.environ`：第三方 MCP server 会因此拿到你所有的 API key
#: （实测：一个恶意 mcp.json 的子进程能读到 `DASHSCOPE_API_KEY`）。
#: 需要额外变量的 server 在配置的 `env` 里显式声明 —— 支持 `${VAR}` 展开，
#: 于是"给哪个变量"是用户写下来的决定，而不是默认全给。
_BASE_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "TZ",
)

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def base_env() -> dict[str, str]:
    """MCP 子进程的基础环境（见 `_BASE_ENV_KEYS`）。"""
    return {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}


def expand_env_refs(values: dict[str, str]) -> dict[str, str]:
    """展开 `env` 里的 `${VAR}` —— 让用户**显式**把某个变量交给 server。

    未定义的引用展开成空串（而不是留下字面量 `${X}`，那会被 server 当成真值）。
    """

    def _sub(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return {k: _ENV_REF.sub(_sub, str(v)) for k, v in values.items()}


def trust_project_mcp(config_value: bool | None = None) -> bool:
    """项目级 MCP 配置是否被信任。

    优先级：显式配置 > 环境变量 > 默认（False）。
    """
    if config_value is not None:
        return bool(config_value)
    return os.environ.get(TRUST_ENV_VAR) == "1"


def load_mcp_config(
    config_path: str | Path | None = None,
    *,
    trust_project: bool | None = None,
    notices: list[str] | None = None,
) -> list[dict[str, Any]]:
    """加载 MCP 配置，返回 server 配置列表。

    Args:
        config_path: 显式路径。传了就**只**加载它，且视为已授权（调用方知道自己在做什么）。
        trust_project: 是否加载工作区的 `.codeforge/mcp.json`；
            None 时走 `trust_project_mcp()`（配置/环境变量/默认 False）。
        notices: 可选，把"跳过了什么、为什么"写进去给用户看。

    Returns:
        server 配置列表；每项含 name / type / command|url 等。
    """
    out = notices if notices is not None else []

    if config_path is not None:
        return _parse(Path(config_path), out)

    servers = _parse(USER_CONFIG_PATH, out, label="用户级")
    if trust_project_mcp(trust_project):
        servers += _parse(_project_path(), out, label="项目级")
    elif _project_path().exists():
        # 只在它**真的声明了 server** 时才提示 —— 空配置（`{"mcpServers": {}}`）
        # 每次都刷一句"已跳过"是噪音，会训练用户忽略启动提示。
        probe: list[str] = []
        declared = _parse(_project_path(), probe, label="项目级")
        if declared:
            out.append(
                f"检测到项目级 MCP 配置 {PROJECT_CONFIG_PATH}（{len(declared)} 个 server），"
                f"**已跳过**（未信任）。它会在会话启动时拉起子进程；"
                f"确认可信后设 features.mcp.trust_project: true "
                f"或环境变量 {TRUST_ENV_VAR}=1。"
            )
        else:
            # 没声明 server，但配置本身有问题（解析失败等）→ 照旧说出来
            out.extend(probe)
    return servers


def _project_path() -> Path:
    return Path.cwd() / PROJECT_CONFIG_PATH


def _parse(path: Path, notices: list[str], label: str = "") -> list[dict[str, Any]]:
    """解析一个 mcp.json；不存在/格式错都返回空列表（不抛）。"""
    if not path.exists():
        logger.debug("MCP config not found: %s", path)
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to parse MCP config %s: %s", path, e)
        notices.append(f"MCP 配置解析失败（{label or path}）：{e}")
        return []

    if not isinstance(data, dict):
        notices.append(f"MCP 配置根节点必须是对象（{label or path}）")
        return []

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        notices.append(f"mcpServers 必须是对象（{label or path}）")
        return []

    configs: list[dict[str, Any]] = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            notices.append(f"跳过 MCP server '{name}'：配置必须是对象")
            continue

        entry: dict[str, Any] = {"name": name, "source": label or str(path)}
        transport_type = cfg.get("type", "stdio")
        entry["type"] = transport_type

        if transport_type == "stdio":
            if "command" not in cfg:
                notices.append(f"跳过 MCP server '{name}'：缺少 'command'")
                continue
            entry["command"] = cfg["command"]
            entry["args"] = cfg.get("args", [])
            entry["env"] = expand_env_refs(cfg.get("env", {}) or {})
        elif transport_type in ("http", "streamableHttp"):
            url = str(cfg.get("url", ""))
            problem = _url_problem(url)
            if problem:
                notices.append(f"跳过 MCP server '{name}'：{problem}")
                continue
            entry["url"] = url
            entry["headers"] = cfg.get("headers", {})
        else:
            notices.append(f"跳过 MCP server '{name}'：未知 type '{transport_type}'")
            continue

        entry["timeout"] = cfg.get("timeout", 60000) / 1000.0  # ms → seconds
        configs.append(entry)

    if configs:
        notices.append(f"MCP 配置（{label or path}）：{len(configs)} 个 server")
    return configs


def _url_problem(url: str) -> str | None:
    """HTTP 传输的 URL 基本校验（**不**限制私网：本机 MCP server 很常见）。"""
    if not url:
        return "缺少 'url'"
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"只支持 http/https，收到 '{parsed.scheme or '(空)'}'"
    if not parsed.hostname:
        return "URL 里没有主机名"
    if parsed.username or parsed.password:
        return "URL 里不该带凭据（把凭据放到 headers 里）"
    return None


def add_user_server(name: str, server: dict[str, Any]) -> Path:
    """把一条 server 配置写进**用户级** `~/.codeforge/mcp.json`（合并，不覆盖别的）。

    刻意只写用户级：项目级配置默认不被信任（见模块 docstring），
    往那儿写等于让用户下次启动看到"已跳过"。

    Returns:
        写入的文件路径。
    """
    path = USER_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            # 坏文件不覆盖 —— 那是用户的东西，宁可报错让他自己看
            raise ValueError(f"{path} 不是合法 JSON，请先修好它再执行本命令") from None

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path} 里的 mcpServers 不是对象")
    servers[name] = server

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def create_default_config(cwd: str | Path = ".") -> Path:
    """创建默认的 .codeforge/mcp.json 配置模板。"""
    path = Path(cwd) / ".codeforge" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        template: dict[str, Any] = {"mcpServers": {}}
        path.write_text(
            json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return path
