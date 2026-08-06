# Tasks

## T1: JSON-RPC 2.0 消息类型

**说明：** 定义 JSON-RPC 2.0 消息类型：`JsonRpcRequest`（id, method, params）、`JsonRpcResponse`（id, result）、`JsonRpcError`（id, error）。含序列化/反序列化（json.dumps/loads）。id 为自增 int。

**影响文件：**
- `core/mcp/__init__.py`
- `core/mcp/types.py` — 消息类型定义 + 编解码

**依赖任务：** 无

**参考资料：**
- JSON-RPC 2.0 spec：request/response/error/notification
- id 可以是 int 或 string，我们用 int

---

## T2: Transport 抽象 + stdio 实现

**说明：** 
- `Transport` 抽象基类：`connect()`, `send(bytes)`, `receive() → bytes`, `close()`
- `StdioTransport`：子进程 stdin/stdout，管理进程生命周期（启动/关闭/kill on timeout），按行读取 JSON（`\n` 分隔）

**影响文件：**
- `core/mcp/transport/__init__.py`
- `core/mcp/transport/base.py` — Transport 抽象
- `core/mcp/transport/stdio.py` — StdioTransport

**依赖任务：** T1（消息类型）

**参考资料：**
- MCP spec：stdio transport 用 `\n` 分隔 JSON 行
- `asyncio.create_subprocess_exec` 启动子进程

---

## T3: Streamable HTTP 实现

**说明：** 实现 `StreamableHTTPTransport`：HTTP POST 发送 JSON-RPC 请求，接收 JSON 响应。支持自定义 headers。用 httpx（已有依赖）。

**影响文件：**
- `core/mcp/transport/http.py` — StreamableHTTPTransport

**依赖任务：** T1（消息类型），T2（base 接口）

**参考资料：**
- MCP spec：Streamable HTTP 用 POST `{url}` 发送 JSON-RPC body
- 响应是 JSON body

---

## T4: MCPClient（生命周期 + JSON-RPC 会话）

**说明：** 实现 MCP 客户端核心：
- 持有 Transport 实例
- `initialize()` → 发送 initialize 请求，协商协议版本和能力
- `list_tools()` → 发送 tools/list → 返回 tool 列表
- `call_tool(name, args)` → 发送 tools/call → 返回执行结果
- 异步请求-响应匹配：`_pending: dict[int, asyncio.Future]`，send 时创建 Future 放入 _pending，receive 循环按 id 取 Future 并 set_result
- 每 30s 发送 ping 保持连接
- `close()` → 关闭 transport

**影响文件：**
- `core/mcp/client.py` — MCPClient

**依赖任务：** T1（types），T2+T3（transport）

**参考资料：**
- MCP lifecycle：initialize → initialized → tools/list → tools/call
- asyncio.Future：id → Future 映射模式

---

## T5: ConnectionPool 连接池

**说明：** 实现连接池：
- `get_client(name) → MCPClient`：懒初始化 + 缓存，已存在的连接直接返回
- 断开后自动重连（最多重试 2 次）
- `close_all()` → 关闭所有连接
- `list_all_tools() → list[dict]`：从所有已连接 server 收集工具列表

**影响文件：**
- `core/mcp/pool.py` — ConnectionPool

**依赖任务：** T4（MCPClient）

**参考资料：**
- 池化策略：按 name 缓存，避免每次调用重连

---

## T6: MCPToolAdapter（MCP tool → CodeForge Tool）

**说明：** 实现 `MCPToolAdapter(Tool)`：
- `name()` → tool_def["name"]
- `description()` → tool_def["description"]，加上 `[MCP: {server_name}]` 前缀标识来源
- `input_schema()` → tool_def["inputSchema"]
- `execute(ctx, input)` → 调用 `client.call_tool(name, input)` → 转成 ToolResult
- `is_read_only()` → True（MCP 工具无法预判是否只读，统一返回 True 避免触发 HITL）
- `is_destructive()` → False
- `category()` → `"mcp"`

**影响文件：**
- `core/mcp/adapter.py` — MCPToolAdapter

**依赖任务：** T4（MCPClient），T5（Pool）

**参考资料：**
- CodeForge Tool 接口：`core/tool/interface.py`
- 现有工具实现：`core/tool/tools/read_file.py` 等

---

## T7: MCP 配置加载器

**说明：** 加载 `.codeforge/mcp.json`，解析 server 列表。验证必填字段（name, type, command for stdio, url for http）。超时默认 60s。

**影响文件：**
- `core/mcp/config.py` — 配置加载

**依赖任务：** 无

**参考资料：**
- Claude Code mcp.json 格式
- 示例配置见 spec

---

## T8: 接入 TUI 主流程

**说明：**
- TUI 启动时加载 `.codeforge/mcp.json`
- 创建 ConnectionPool → 逐个 server 初始化连接 → 发现工具 → 包装为 MCPToolAdapter → 注册到 ToolRegistry
- 连接失败或配置缺失不中断启动（相关工具跳过）
- 启动 banner 打印已加载的 MCP server/tool 数量
- 程序退出时调用 `pool.close_all()` 清理连接

**影响文件：**
- `tui/app.py` — 修改

**依赖任务：** T6（adapter），T7（config）

**参考资料：**
- 现有 `tui/app.py` 中 `get_default_registry()` 调用处

---

## T9: 端到端验证

**说明：**
- mock JSON-RPC server（用 asyncio 子进程模拟 initialize + tools/list + tools/call）
- 测试：配置加载、连接初始化、工具发现、工具调用、连接池复用、断线重连、超时处理
- 确认 Agent 通过 MCP 工具完成任务

**影响文件：**
- `tests/test_mcp.py` — 新增

**依赖任务：** T8（全部集成）

**参考资料：**
- checklist.md 验收项
