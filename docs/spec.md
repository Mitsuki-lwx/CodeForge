# CodeForge MCP Client · 规格说明

## 背景

CodeForge 当前只有内置的 7 个工具（read_file, write_file, edit_file, bash, glob, grep, ExitPlanMode）。要扩展能力只能改代码加工具。MCP (Model Context Protocol) 是 Anthropic 提出的开放协议，允许 AI 应用通过标准 JSON-RPC 2.0 接口连接外部工具服务器。

实现 MCP 客户端后，CodeForge 可以连接任意的 MCP server（文件系统服务器、web 搜索服务器、数据库查询服务器等），把远端工具无缝接入 Agent Loop。

## 目标用户

CodeForge 终端用户，通过配置 `.codeforge/mcp.json` 扩展可用工具集。

## 能力清单

1. **JSON-RPC 2.0 消息协议** — 请求/响应/错误/通知四类消息按规范编解码，id 为自增整数
2. **stdio 传输** — 子进程 stdin/stdout，支持 command + args 启动，自动管理进程生命周期
3. **Streamable HTTP 传输** — 通过 HTTP POST 发送 JSON-RPC，支持自定义 headers
4. **MCP 生命周期** — 三阶段：initialize 握手（能力协商）→ tools/list 发现 → tools/call 调用
5. **异步请求-响应匹配** — 每个请求带唯一 id，用 `asyncio.Future` 做 id → Future mapping，回包按 id 关联
6. **Tool 接口适配** — MCP 工具包装成 CodeForge `Tool` 接口（name/description/input_schema/execute），注册进 ToolRegistry，Agent 调用无感
7. **连接池化** — 多个 server 的连接建立后复用，避免每次工具调用都重连；断线自动重连
8. **独立配置文件** — `.codeforge/mcp.json` 声明 server 列表（command/args/env/type/url/headers/timeout）

## 非功能要求

- **超时控制** — 每个 JSON-RPC 调用受 timeout 约束（默认 60s），超时返回错误不阻塞 Agent
- **降级容错** — server 不可用、进程崩溃、网络不通时不中断会话，相关工具标记为不可用
- **stdio 安全** — 子进程正确关闭 stdin/stdout/stderr；进程退出时清理资源
- **不破坏 ch04-ch06** — Agent Loop、权限检查、缓存体系不退化
- **日志可观测** — JSON-RPC 通信异常记录到 logger，方便排查 MCP 集成问题

## 设计骨架

```
core/mcp/
├── __init__.py           # 导出
├── types.py              # JSON-RPC 2.0 消息类型定义
├── transport/
│   ├── __init__.py
│   ├── base.py           # Transport 抽象基类
│   ├── stdio.py          # StdioTransport（子进程 stdin/stdout）
│   └── http.py           # StreamableHTTPTransport（HTTP POST）
├── client.py             # MCPClient（连接生命周期 + JSON-RPC 会话）
├── pool.py               # ConnectionPool（多 server 连接缓存/池化）
├── adapter.py            # MCPToolAdapter（MCP tool → CodeForge Tool 接口）
└── config.py             # MCP 配置加载器（.codeforge/mcp.json）

tui/app.py                # (修改) 初始化时加载 MCP server 并注册工具
```

### 核心类型

**JSON-RPC 消息**:
```python
@dataclass
class JsonRpcRequest:
    jsonrpc: str = "2.0"
    id: int = 0
    method: str = ""
    params: dict | None = None

@dataclass
class JsonRpcResponse:
    jsonrpc: str = "2.0"
    id: int = 0
    result: Any = None

@dataclass
class JsonRpcError:
    jsonrpc: str = "2.0"
    id: int = 0
    error: dict | None = None  # {code, message, data}
```

**Transport 接口**:
```python
class Transport(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def send(self, message: bytes) -> None: ...
    @abstractmethod
    async def receive(self) -> bytes: ...
    @abstractmethod
    async def close(self) -> None: ...
```

**MCPClient**:
```python
class MCPClient:
    def __init__(self, transport: Transport): ...
    async def initialize(self) -> dict: ...       # → capabilities
    async def list_tools(self) -> list[dict]: ...  # → tool definitions
    async def call_tool(self, name: str, args: dict) -> dict: ...
    async def close(self) -> None: ...

    # Internal: id → Future mapping
    _pending: dict[int, asyncio.Future]
    _next_id: int
```

**MCPToolAdapter**:
```python
class MCPToolAdapter(Tool):
    def __init__(self, client: MCPClient, tool_def: dict): ...
    def name(self) -> str:          # from tool_def["name"]
    def description(self) -> str:   # from tool_def["description"]
    def input_schema(self) -> dict: # from tool_def["inputSchema"]
    async def execute(self, ctx, input) -> ToolResult:  # → tools/call
```

**ConnectionPool**:
```python
class ConnectionPool:
    def __init__(self): ...
    async def get_client(self, name: str) -> MCPClient:
        """获取或创建连接（懒初始化 + 缓存）。"""
    async def close_all(self) -> None: ...
```

### 连接流程

```
加载 .codeforge/mcp.json
    ↓
对每个 server entry:
    ├─ type=stdio  → StdioTransport(command, args, env) → 启动子进程
    └─ type=http   → StreamableHTTPTransport(url, headers)
    ↓
MCPClient(transport)
    ↓
1. handshake:
    send: {"method": "initialize", "params": {...capabilities...}}
    recv: {"result": {"protocolVersion": "2024-11-05", "capabilities": {...}}}
    send: {"method": "notifications/initialized"}  (通知，无 id)
    ↓
2. tool discovery:
    send: {"method": "tools/list"}
    recv: {"result": {"tools": [{name, description, inputSchema}, ...]}}
    ↓
3. wrap tools:
    每个 tool def → MCPToolAdapter(client, def) → ToolRegistry.register()
    ↓
Agent 调用时:
    ToolRegistry.execute(name, ctx, input)
    → MCPToolAdapter.execute(ctx, input)
    → MCPClient.call_tool(name, input)
    → JSON-RPC tools/call → server → result
```

### 配置文件格式（`.codeforge/mcp.json`）

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": {},
      "timeout": 60000
    },
    "web-search": {
      "type": "streamableHttp",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer xxx"},
      "timeout": 30000
    }
  }
}
```

- `type`: `"stdio"` (默认) 或 `"streamableHttp"`
- `timeout`: 默认 60s，每 server 可覆盖
- `env`: stdio 模式下的额外环境变量

## Out of Scope

- **MCP server 实现** — 只做客户端，不实现服务端
- **MCP 协议其他能力** — resources、prompts、sampling 不做，只做 tools
- **动态 server 注册/热加载** — 配置只读，修改后需重启
- **server 间路由/负载均衡** — 不做
- **MCP 认证/授权标准** — header 透传即可，不做 OAuth 流程
- **工具 Schema 校验缓存** — 每次调用按 MCP 返回的 inputSchema 校验
