"""MCP Client 集成测试。"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.mcp.adapter import MCPToolAdapter
from core.mcp.client import MCPClient
from core.mcp.config import load_mcp_config
from core.mcp.pool import ConnectionPool
from core.mcp.transport.stdio import StdioTransport
from core.mcp.types import JsonRpcRequest, JsonRpcResponse, parse_message
from core.tool.context import ExecutionContext
from core.tool.registry import ToolRegistry

MOCK_SERVER_SCRIPT = str(Path(__file__).parent / "mock_mcp_server.py")


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def mock_transport():
    """Create a stdio transport to the mock MCP server."""
    return StdioTransport(
        command=sys.executable,
        args=[MOCK_SERVER_SCRIPT],
    )


@pytest.fixture
async def mcp_client(mock_transport):
    """Create and initialize an MCP client connected to mock server."""
    client = MCPClient(mock_transport, server_name="mock")
    await client.connect()
    caps = await client.initialize()
    assert "tools" in caps.get("capabilities", {})
    yield client
    await client.close()


# ── JSON-RPC Types ─────────────────────────────────────────────


class TestJsonRpcTypes:
    def test_request_serialize(self):
        req = JsonRpcRequest(method="tools/list", id=1)
        data = req.serialize()
        assert b'"jsonrpc"' in data
        assert b'"method"' in data
        assert b'"tools/list"' in data
        assert b'"id"' in data

    def test_request_with_params(self):
        req = JsonRpcRequest(method="tools/call", params={"name": "echo"}, id=2)
        data = req.serialize()
        obj = json.loads(data)
        assert obj["params"] == {"name": "echo"}

    def test_parse_response(self):
        data = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
        msg = parse_message(data)
        assert isinstance(msg, JsonRpcResponse)
        assert msg.id == 1
        assert msg.result == {"tools": []}

    def test_parse_error(self):
        data = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"Invalid"}}'
        msg = parse_message(data)
        from core.mcp.types import JsonRpcError

        assert isinstance(msg, JsonRpcError)
        assert msg.error["code"] == -32600

    def test_parse_notification_returns_none(self):
        data = b'{"jsonrpc":"2.0","method":"notifications/initialized"}'
        msg = parse_message(data)
        assert msg is None

    def test_notification_serialize_no_id(self):
        from core.mcp.types import JsonRpcNotification

        notif = JsonRpcNotification(method="notifications/initialized")
        data = notif.serialize()
        assert b'"id"' not in data


# ── MCPClient ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMCPClient:
    async def test_initialize(self, mcp_client):
        caps = await mcp_client.initialize()
        assert caps["protocolVersion"] == "2024-11-05"

    async def test_list_tools(self, mcp_client):
        tools = await mcp_client.list_tools()
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert names == {"echo", "add"}

    async def test_call_echo(self, mcp_client):
        result = await mcp_client.call_tool("echo", {"message": "hello"})
        content = result["content"]
        assert len(content) == 1
        assert "hello" in content[0]["text"]

    async def test_call_add(self, mcp_client):
        result = await mcp_client.call_tool("add", {"a": 3, "b": 5})
        text = result["content"][0]["text"]
        assert "8" in text

    async def test_concurrent_calls(self, mcp_client):
        """并发调用：多个请求按 id 正确匹配。"""

        async def call_add(a, b):
            return await mcp_client.call_tool("add", {"a": a, "b": b})

        results = await asyncio.gather(
            call_add(1, 2),
            call_add(3, 4),
            call_add(5, 6),
        )
        assert "3" in results[0]["content"][0]["text"]
        assert "7" in results[1]["content"][0]["text"]
        assert "11" in results[2]["content"][0]["text"]


# ── MCPToolAdapter ────────────────────────────────────────────


@pytest.mark.asyncio
class TestMCPToolAdapter:
    async def test_adapter_name(self, mcp_client):
        tools = await mcp_client.list_tools()
        echo_def = next(t for t in tools if t["name"] == "echo")
        adapter = MCPToolAdapter(mcp_client, echo_def, "mock")
        assert adapter.name() == "mock__echo"

    async def test_adapter_description(self, mcp_client):
        tools = await mcp_client.list_tools()
        echo_def = next(t for t in tools if t["name"] == "echo")
        adapter = MCPToolAdapter(mcp_client, echo_def, "mock")
        assert "[MCP:mock]" in adapter.description()

    async def test_adapter_execute(self, mcp_client):
        tools = await mcp_client.list_tools()
        add_def = next(t for t in tools if t["name"] == "add")
        adapter = MCPToolAdapter(mcp_client, add_def, "mock")

        ctx = ExecutionContext(cwd=Path("."), session_id="test")
        result = await adapter.execute(ctx, {"a": 10, "b": 20})
        assert result.success
        assert "30" in str(result.data)

    async def test_adapter_registry_integration(self, mcp_client):
        """MCPToolAdapter 注册到 ToolRegistry，普通调用正常。"""
        tools = await mcp_client.list_tools()
        echo_def = next(t for t in tools if t["name"] == "echo")
        add_def = next(t for t in tools if t["name"] == "add")

        registry = ToolRegistry()
        registry.register(MCPToolAdapter(mcp_client, echo_def, "mock"))
        registry.register(MCPToolAdapter(mcp_client, add_def, "mock"))

        ctx = ExecutionContext(cwd=Path("."), session_id="test")
        result = await registry.execute("mock__add", ctx, {"a": 7, "b": 3})
        assert result.success
        assert "10" in str(result.data)


# ── ConnectionPool ──────────────────────────────────────────


@pytest.mark.asyncio
class TestConnectionPool:
    async def test_get_client_cached(self, mcp_client):
        """Pool 缓存已创建的连接。"""
        pool = ConnectionPool()
        pool._server_configs["mock"] = {
            "name": "mock",
            "type": "stdio",
            "command": sys.executable,
            "args": [MOCK_SERVER_SCRIPT],
        }
        # 直接注入 client
        pool._clients["mock"] = mcp_client

        c1 = await pool.get_client("mock")
        c2 = await pool.get_client("mock")
        assert c1 is c2

    async def test_list_all_tools(self, mcp_client):
        pool = ConnectionPool()
        pool._server_configs["mock"] = {
            "name": "mock",
            "type": "stdio",
            "command": sys.executable,
            "args": [MOCK_SERVER_SCRIPT],
        }
        pool._clients["mock"] = mcp_client

        all_tools = await pool.list_all_tools()
        assert "mock" in all_tools
        assert len(all_tools["mock"]) == 2


# ── Config ─────────────────────────────────────────────────


class TestMCPConfig:
    def test_load_nonexistent(self, tmp_path):
        configs = load_mcp_config(tmp_path / "nonexistent.json")
        assert configs == []

    def test_load_empty(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text('{"mcpServers": {}}')
        configs = load_mcp_config(path)
        assert configs == []

    def test_load_stdio_server(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "test-srv": {
                            "command": "python",
                            "args": ["-c", "print('hello')"],
                        }
                    }
                }
            )
        )
        configs = load_mcp_config(path)
        assert len(configs) == 1
        assert configs[0]["name"] == "test-srv"
        assert configs[0]["command"] == "python"
        assert configs[0]["type"] == "stdio"

    def test_load_http_server(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "web": {
                            "type": "streamableHttp",
                            "url": "https://example.com/mcp",
                            "headers": {"Auth": "token"},
                        }
                    }
                }
            )
        )
        configs = load_mcp_config(path)
        assert len(configs) == 1
        assert configs[0]["url"] == "https://example.com/mcp"

    def test_skip_invalid(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "ok": {"command": "ls"},
                        "bad": {},  # missing command
                        "bad2": "not-an-object",
                    }
                }
            )
        )
        configs = load_mcp_config(path)
        assert len(configs) == 1
        assert configs[0]["name"] == "ok"


# ── 只读判定：按服务端自述的 annotations（修的是一个 fail-open）──────


class TestMCPReadOnlyInference:
    """`is_read_only()` 曾经**无条件返回 True**，于是每个 MCP 工具都被
    `normalize_tool_category("mcp", True)` 归成 `read` 档 → Layer 1 直接放行，
    连审批都不进。等于"挂一个 MCP server = 无条件授权它全部工具"。

    现在按 MCP 规范的 `annotations.readOnlyHint` 判；**没声明就算非只读**。
    """

    def _adapter(self, annotations=None):
        defn = {"name": "t", "description": "d", "inputSchema": {}}
        if annotations is not None:
            defn["annotations"] = annotations
        return MCPToolAdapter(object(), defn, "srv")

    def test_declared_read_only_is_read_only(self):
        assert self._adapter({"readOnlyHint": True}).is_read_only() is True

    def test_missing_annotation_is_not_read_only(self):
        """★ 没声明 → 非只读（走 command 档，要过审批）。"""
        assert self._adapter().is_read_only() is False

    def test_explicit_false_is_not_read_only(self):
        assert self._adapter({"readOnlyHint": False}).is_read_only() is False

    def test_malformed_annotations_do_not_crash(self):
        assert self._adapter("not-a-dict").is_read_only() is False

    def test_destructive_hint(self):
        assert self._adapter({"destructiveHint": True}).is_destructive() is True
        assert self._adapter({}).is_destructive() is False

    def test_concurrency_follows_read_only(self):
        """并发安全跟着只读走 —— 非只读的 MCP 工具必须串行（否则互相改状态）。"""
        assert self._adapter({"readOnlyHint": True}).is_concurrency_safe({}) is True
        assert self._adapter({}).is_concurrency_safe({}) is False

    def test_category_still_mcp(self):
        """category 不变 —— 由 `normalize_tool_category` 按只读与否分流。"""
        from core.agent.tool_category import normalize_tool_category

        a = self._adapter({"readOnlyHint": True})
        b = self._adapter()
        assert a.category() == b.category() == "mcp"
        assert normalize_tool_category(a.category(), a.is_read_only()) == "read"
        assert normalize_tool_category(b.category(), b.is_read_only()) == "command"
