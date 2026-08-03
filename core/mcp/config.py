"""MCP 配置加载器 —— 解析 .codeforge/mcp.json。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_mcp_config(config_path: str | Path = ".codeforge/mcp.json") -> list[dict[str, Any]]:
    """加载 MCP 配置文件，返回 server 配置列表。

    Args:
        config_path: 配置文件路径（相对于工作目录或绝对路径）

    Returns:
        server 配置列表，每项含 name, type, command/url 等字段。
        配置文件不存在或格式错误时返回空列表。
    """
    path = Path(config_path)
    if not path.is_absolute():
        path = Path.cwd() / path

    if not path.exists():
        logger.debug("MCP config not found: %s", path)
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to parse MCP config %s: %s", path, e)
        return []

    if not isinstance(data, dict):
        logger.warning("MCP config root must be an object")
        return []

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        logger.warning("mcpServers must be an object")
        return []

    configs = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            logger.warning("Skipping MCP server '%s': config must be an object", name)
            continue

        entry: dict[str, Any] = {"name": name}
        transport_type = cfg.get("type", "stdio")
        entry["type"] = transport_type

        if transport_type == "stdio":
            if "command" not in cfg:
                logger.warning("Skipping MCP server '%s': missing 'command'", name)
                continue
            entry["command"] = cfg["command"]
            entry["args"] = cfg.get("args", [])
            entry["env"] = cfg.get("env", {})
        elif transport_type in ("http", "streamableHttp"):
            if "url" not in cfg:
                logger.warning("Skipping MCP server '%s': missing 'url'", name)
                continue
            entry["url"] = cfg["url"]
            entry["headers"] = cfg.get("headers", {})
        else:
            logger.warning("Skipping MCP server '%s': unknown type '%s'", name, transport_type)
            continue

        entry["timeout"] = cfg.get("timeout", 60000) / 1000.0  # ms → seconds
        configs.append(entry)

    return configs


def create_default_config(cwd: str | Path = ".") -> Path:
    """创建默认的 .codeforge/mcp.json 配置模板。"""
    path = Path(cwd) / ".codeforge" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        template = {
            "mcpServers": {}
        }
        path.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
