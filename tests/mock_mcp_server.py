"""Mock MCP Server — 实现最小 MCP 协议用于集成测试。

通过 stdio 通信：读 JSON-RPC 行，返回 JSON-RPC 响应。
实现 MCP 三个必需方法：initialize, tools/list, tools/call。
"""

from __future__ import annotations

import json
import sys


def run_mock_server() -> None:
    """Run a minimal MCP-compliant server on stdin/stdout.

    Supports: initialize, tools/list, tools/call (echo + add tools).
    """
    _log("Mock MCP server started")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _log(f"Bad JSON: {line[:80]}")
            continue

        if request.get("jsonrpc") != "2.0":
            continue

        method = request.get("method", "")
        req_id = request.get("id", 0)
        params = request.get("params", {})

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-server", "version": "1.0.0"},
            }
            _send(req_id, result)

            # Wait for initialized notification
            for nline in sys.stdin:
                nline = nline.strip()
                if not nline:
                    continue
                try:
                    nreq = json.loads(nline)
                except json.JSONDecodeError:
                    continue
                if nreq.get("method") == "notifications/initialized":
                    break

        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo back the input message",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string", "description": "The message to echo"}
                            },
                            "required": ["message"],
                        },
                    },
                    {
                        "name": "add",
                        "description": "Add two numbers together",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "a": {"type": "number", "description": "First number"},
                                "b": {"type": "number", "description": "Second number"},
                            },
                            "required": ["a", "b"],
                        },
                    },
                ]
            }
            _send(req_id, result)

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if tool_name == "echo":
                msg = arguments.get("message", "")
                result = {
                    "content": [{"type": "text", "text": f"Echo: {msg}"}],
                }
            elif tool_name == "add":
                a = arguments.get("a", 0)
                b = arguments.get("b", 0)
                result = {
                    "content": [{"type": "text", "text": f"{a} + {b} = {a + b}"}],
                }
            else:
                result = {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                }
            _send(req_id, result)

        elif method == "tools/call_timeout":
            # Simulate a slow tool
            import time
            time.sleep(999)
            return

        elif method == "notifications/initialized":
            pass  # Notification, no response

        else:
            error = {"code": -32601, "message": f"Method not found: {method}"}
            _send_err(req_id, error)


def _send(req_id: int, result: dict) -> None:
    response = {"jsonrpc": "2.0", "id": req_id, "result": result}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send_err(req_id: int, error: dict) -> None:
    response = {"jsonrpc": "2.0", "id": req_id, "error": error}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    sys.stderr.write(f"[mock-mcp] {msg}\n")
    sys.stderr.flush()


if __name__ == "__main__":
    run_mock_server()
