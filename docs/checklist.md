# Checklist

## T1: JSON-RPC 2.0 消息类型

- [ ] `JsonRpcRequest(jsonrpc="2.0", id=1, method="tools/list")` 可构造
- [ ] `JsonRpcResponse(id=1, result={...})` 可构造
- [ ] `JsonRpcError(id=1, error={"code": -32600, "message": "Invalid Request"})` 可构造
- [ ] request.serialize() → `{"jsonrpc":"2.0","id":1,"method":"tools/list","params":null}`
- [ ] notification（id=0 或无 id）→ serialize 时不带 id 字段
- [ ] `from core.mcp import JsonRpcRequest, JsonRpcResponse` 可导入

## T2: Transport 抽象 + stdio 实现

- [ ] `StdioTransport.__init__(command="echo", args=["hello"])` 构造成功
- [ ] `await transport.connect()` → 子进程启动
- [ ] `await transport.send(b'{"jsonrpc":"2.0"}\n')` → 写入子进程 stdin
- [ ] `await transport.receive()` → 从子进程 stdout 读一行 JSON
- [ ] 子进程退出 → `receive()` 抛异常
- [ ] `await transport.close()` → stdin 关闭、进程等待退出
- [ ] 超时未响应 → close 时 force kill

## T3: Streamable HTTP 实现

- [ ] `StreamableHTTPTransport(url="http://localhost:9999/mcp")` 构造成功
- [ ] `await transport.send(json_bytes)` → HTTP POST 请求发出
- [ ] `await transport.receive()` → 返回 HTTP 响应 body
- [ ] HTTP 错误状态码 → 抛异常带 status_code
- [ ] 自定义 headers 随请求发送
- [ ] 网络不通 → 抛连接异常

## T4: MCPClient

- [ ] `await client.initialize()` → 发送 initialize → 返回 server capabilities
- [ ] initialize 后自动发送 `notifications/initialized`
- [ ] `await client.list_tools()` → 发送 tools/list → 返回工具定义列表
- [ ] `await client.call_tool("echo", {"text": "hello"})` → 发送 tools/call → 返回执行结果
- [ ] 并发 3 个 call_tool → 3 个请求按 id 正确匹配各自响应
- [ ] server 返回 error → call_tool 抛异常带 error 信息
- [ ] 超时 → call_tool 抛 TimeoutError
- [ ] `await client.close()` → transport 关闭、pending futures 全部 cancel

## T5: ConnectionPool

- [ ] `pool.get_client("filesystem")` → 首次调用创建新连接并初始化
- [ ] 再次 `pool.get_client("filesystem")` → 返回缓存连接（不重新初始化）
- [ ] 两个不同 name → 各自独立连接
- [ ] 连接断开后下次 get_client → 自动重连（最多 2 次重试）
- [ ] `pool.list_all_tools()` → 返回 `{server_name: [tool_defs]}` 字典
- [ ] `await pool.close_all()` → 所有连接关闭

## T6: MCPToolAdapter

- [ ] `adapter.name()` → 返回 MCP tool name（不含 server 前缀）
- [ ] `adapter.description()` → 包含 `[MCP: {server_name}]` 前缀
- [ ] `adapter.input_schema()` → 返回 MCP tool inputSchema
- [ ] `await adapter.execute(ctx, {"param": "value"})` → 调用 client.call_tool → 返回 ToolResult
- [ ] call_tool 返回错误 → ToolResult(success=False, error=...)
- [ ] `adapter.is_read_only()` → True
- [ ] `adapter.category()` → `"mcp"`
- [ ] MCPToolAdapter 注册到 ToolRegistry → Agent 可调用

## T7: MCP 配置加载

- [ ] `load_mcp_config(".codeforge/mcp.json")` → 返回 server 配置列表
- [ ] 配置文件不存在 → 返回空列表（不报错）
- [ ] JSON 格式损坏 → 返回空列表 + logger.warning
- [ ] 缺少必填字段（name）→ 跳过该 server + logger.warning
- [ ] stdio server 缺少 command → 跳过 + warning
- [ ] http server 缺少 url → 跳过 + warning
- [ ] timeout 默认 60000ms
- [ ] 配置格式与 Claude Code mcp.json 兼容

## T8: 接入 TUI 主流程

- [ ] 启动时加载 `.codeforge/mcp.json`
- [ ] 每个 server 初始化连接 → 发现工具 → 包装 → 注册到 ToolRegistry
- [ ] 连接失败的 server 跳过，不影响启动
- [ ] 启动 banner 打印 `MCP: N servers, M tools loaded`
- [ ] `grep -r "close_all" tui/app.py` 确认退出时清理连接
- [ ] 无 `.codeforge/mcp.json` 时程序正常启动（无 MCP 工具）

## T9: 端到端验证

- [ ] `pytest tests/test_mcp.py -v` 全部通过
- [ ] `pytest tests/ -v` 全量 130+ 通过
- [ ] Mock server：echo tool → Agent 调用 → 返回结果
- [ ] Mock server 返回错误 → Agent 收到 ToolResult(success=False)
- [ ] Mock server 超时 → Agent 收到超时错误
- [ ] 两个 mock server → ToolRegistry 包含两个 server 的工具，名称不冲突
- [ ] `ruff check .` 无告警；`ruff format --check .` 通过
