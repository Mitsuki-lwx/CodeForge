# Tasks — 上下文压缩

本章按 spec 模块拆分实现任务，每个任务自包含、可在一次专注会话内完成。任务间通过"依赖"标明顺序。

## 文件清单

| 文件路径 | 类型 | 职责 |
|----------|------|------|
| `core/context_compression/__init__.py` | 新建 | 重导出 manage_context / TriggerKind / State 类型 |
| `core/context_compression/const.py` | 新建 | 全部硬编码常量 |
| `core/context_compression/state.py` | 新建 | ContentReplacementState / CompactCircuitBreaker / RecoveryState / SessionContext |
| `core/context_compression/token.py` | 新建 | estimate_tokens / usage_anchor / message_chars |
| `core/context_compression/layer1.py` | 新建 | offload_and_snip / spill_single / build_preview |
| `core/context_compression/summary_prompt.py` | 新建 | build_summary_prompt / serialize_conversation / extract_summary |
| `core/context_compression/recovery.py` | 新建 | build_recovery_attachment / render_file_block / render_tools_block / BOUNDARY_NOTICE |
| `core/context_compression/layer2.py` | 新建 | auto_compact / force_compact / run_summary / summarize_once / ptl_retry / pick_recent_tail / group_by_user_turn |
| `core/context_compression/compact.py` | 新建 | manage_context / TriggerKind / ManageInput / ManageOutput |
| `tests/test_context_compression.py` | 新建 | 全部 compact 子包单测 |
| `llm/__init__.py` | 修改 | 新增 PromptTooLongError 哨兵异常 |
| `llm/anthropic_client.py` | 修改 | 上下文过长异常 → PromptTooLongError 包装 |
| `llm/openai_client.py` | 修改 | 同上 |
| `conversation/manager.py` | 修改 | 新增 replace_history() 深拷贝整体替换 |
| `config/model.py` | 修改 | ProviderConfig 追加 context_window 字段 |
| `config/protocol_defaults.py` | 新建 | 协议默认窗口值常量 |
| `core/agent/runtime.py` | 新建 | SessionRuntime dataclass |
| `core/agent/event.py` | 新建/修改 | CompactPhase / CompactEvent；Event 追加 compact 字段 |
| `core/agent/agent.py` | 修改 | 主循环集成 / ReadFile 追踪 / 紧急压缩 / run_force_compact |
| `tui/commands.py` | 新建 | 命令分发 + /compress / /exit / /plan / /do |
| `tui/app.py` | 修改 | 持有 SessionRuntime 与 Agent |

---

## T1: 常量定义

**说明：** 创建 `const.py`，定义全部硬编码常量。

**影响文件：**
- `core/context_compression/__init__.py` — 新建空文件
- `core/context_compression/const.py` — 新建

**依赖任务：** 无

**参考资料：**
- spec_compress.md 核心常量表（14 个常量）
- 每个常量上一行中文注释说明含义

---

## T2: SessionContext 与目录创建

**说明：** 创建 `state.py`，定义 `SessionContext` dataclass 和 `new_session_context(workspace)` 工厂函数：
- `session_id` 格式：`{int(time.time())}-{secrets.token_hex(4)}`
- `spill_dir` 指向 `.codeforge/sessions/{session_id}/tool-results/`
- `Path(spill_dir).mkdir(parents=True, exist_ok=True)`

**影响文件：**
- `core/context_compression/state.py` — 新建

**依赖任务：** T1

**参考资料：**
- spec_compress.md SessionContext 定义
- `secrets.token_hex(4)` 生成 8 字符随机串

---

## T3: ContentReplacementState 与 CompactCircuitBreaker

**说明：** 在 `state.py` 中追加：
- `ContentReplacementState`：`_seen_ids: set[str]` + `_replacements: dict[str, str]`，唯一高层方法 `decide_once(id, original, decide) → str`，原子完成查账本→回调决策→写账本。已 Seen id 直接返回存量结果、不重新调回调、不重新构造 preview。
- `CompactCircuitBreaker`：`_consecutive_failures`，`record_success()` / `record_failure()` / `tripped() → bool`（≥ 3 次返回 True）

**影响文件：**
- `core/context_compression/state.py` — 追加

**依赖任务：** T2

**参考资料：**
- spec_compress.md ContentReplacementState 接口
- Python asyncio 单线程事件循环保证串行，无需显式锁

---

## T4: RecoveryState 与 FileReadRecord

**说明：** 在 `state.py` 中追加：
- `FileReadRecord` dataclass：`path: str` / `content: str` / `timestamp: datetime`
- `RecoveryState`：`_files: dict[str, FileReadRecord]`（键为绝对路径）
- `record_file(path, content)`：非绝对路径则 resolve；timestamp = datetime.now()
- `snapshot() → list[FileReadRecord]`：按 timestamp 倒序排序的拷贝列表

**影响文件：**
- `core/context_compression/state.py` — 追加

**依赖任务：** T3

**参考资料：**
- spec_compress.md RecoveryState 定义

---

## T5: Token 估算

**说明：** 创建 `token.py`：
- `usage_anchor(u: dict) → int`：`u["input_tokens"] + u["output_tokens"] + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)`
- `message_chars(msgs: list[Message]) → int`：累加每条消息 content 的 `len(content.encode("utf-8"))`（含 tool_use input 和 tool_result content 的字节长度）
- `estimate_tokens(anchor: int, all_msgs: list[Message], anchor_msg_len: int) → int`：`anchor + ceil(message_chars(all_msgs[anchor_msg_len:]) / 3.5)`

**影响文件：**
- `core/context_compression/token.py` — 新建

**依赖任务：** T1

**参考资料：**
- spec_compress.md Token 估算公式
- conversation/message.py Message 类型

---

## T6: 单条工具结果落盘 spill_single

**说明：** 创建 `layer1.py`：
- `spill_single(session: SessionContext, tool_use_id: str, content: str) → None`：拼接 `Path(session.spill_dir) / tool_use_id`，已存在则幂等返回，否则 `path.write_bytes(content.encode("utf-8"))`
- `_head_preview(content: str) → str`：按 `\n` 切最多 20 行，再按 2048 字节二次裁剪
- `build_preview(original_bytes: int, head: str, spill_path: str) → str`：固定格式多行字符串，含原始字节数、落盘路径、头部预览、重读提示

**影响文件：**
- `core/context_compression/layer1.py` — 新建

**依赖任务：** T2

**参考资料：**
- spec_compress.md 预览体格式
- `PREVIEW_HEAD_BYTES = 2048`，`PREVIEW_HEAD_LINES = 20`

---

## T7: offload_and_snip 主体

**说明：** 在 `layer1.py` 中实现：
- `offload_and_snip(msgs: list[Message], state: ContentReplacementState, session: SessionContext) → list[Message]`
- 深拷贝 msgs → out
- 仅遍历 `msg.role == "user"` 且有 tool_result 的消息（CodeForge 中 tool_result 挂在 user 消息的 content blocks 中）
- 对每条 tool 消息内每个 tool_result：
  - 已 Seen 的通过 `decide_once` 复用存量结果
  - 未决策的进入候选列表，按字节倒序处理
  - 单条超限落盘、聚合超限依次落盘、未落盘的 kept
  - 落盘→改写 content→写账本通过 `decide_once` 在同一临界区完成
  - 落盘失败（OSError）→ 返回 "skip"，不写账本

**影响文件：**
- `core/context_compression/layer1.py` — 追加

**依赖任务：** T3, T6

**参考资料：**
- spec_compress.md 第一层流程
- conversation/message.py `make_tool_result_block`
- `SINGLE_RESULT_LIMIT = 50000`，`MESSAGE_AGGREGATE_LIMIT = 200000`

---

## T8: 摘要 Prompt 模板与解析

**说明：** 创建 `summary_prompt.py`：
- 模块级常量 `SUMMARY_INSTRUCTION`：含两阶段指令（`<analysis>` / `<summary>`）、9 个小节标题、首尾禁止工具调用声明
- `serialize_conversation(msgs: list[Message]) → str`：扁平化对话为可读文本
- `build_summary_prompt(msgs: list[Message]) → list[Message]`：返回 1 条 user 消息
- `extract_summary(raw: str) → str`：正则提取 `<summary>...</summary>`，失败返回原文

**影响文件：**
- `core/context_compression/summary_prompt.py` — 新建

**依赖任务：** T1

**参考资料：**
- spec_compress.md 摘要 Prompt 结构（9 分区完整文案）
- 9 个小节标题用固定字面字符串

---

## T9: 恢复三段 — 文件块与工具块渲染

**说明：** 创建 `recovery.py`：
- `BOUNDARY_NOTICE`：模块级常量，固定边界提示文案
- `render_file_block(rec: FileReadRecord) → str`：渲染单文件快照，超过 5,000 token 时保留头部、截尾部、追加 `(content truncated)`
- `render_tools_block(defs: list[dict]) → str`：每行一个工具名 + description + input_schema JSON
- `build_recovery_attachment(snapshot: list[FileReadRecord], tool_defs: list[dict]) → str`：纯函数，拼接三段

**影响文件：**
- `core/context_compression/recovery.py` — 新建

**依赖任务：** T4

**参考资料：**
- spec_compress.md 恢复三段结构
- `RECOVERY_FILE_LIMIT = 5`，`RECOVERY_TOKENS_PER_FILE = 5000`

---

## T10: pick_recent_tail + group_by_user_turn

**说明：** 创建 `layer2.py`：
- `pick_recent_tail(msgs: list[Message]) → list[Message]`：从尾到头累加，token ≥ 10,000 且条数 ≥ 5 后停手（双下界择宽）；再做 tool_use/tool_result 配对修正（截断点在 tool_result 时前推到配对 tool_use 之前）
- `_join_after_summary(summary_msg: Message, recent: list[Message]) → list[Message]`：处理 role 衔接，recent 首条为 user 时插入 assistant 占位
- `group_by_user_turn(msgs: list[Message]) → list[list[Message]]`：按 "user → 后续 assistant/tool" 分组

**影响文件：**
- `core/context_compression/layer2.py` — 新建

**依赖任务：** T5

**参考资料：**
- spec_compress.md 近期原文保留规则
- `RECENT_KEEP_TOKENS = 10000`，`RECENT_KEEP_MESSAGES = 5`

---

## T11: summarize_once + ptl_retry

**说明：** 在 `layer2.py` 中追加：
- `async summarize_once(in_: ManageInput, msgs: list[Message]) → str`：构造摘要请求（tools=None），流式收集文本，尾事件 usage 不更新 anchor；PTL 异常透传；返回 `extract_summary(text)`
- `async ptl_retry(in_: ManageInput, msgs: list[Message], first_err: Exception) → str`：`group_by_user_turn` → 前 3 次每次丢最旧 1 组 → 之后按 `ceil(剩余 × 20%)` 丢（至少 1 组） → 全部丢光抛最后异常；中间非 PTL 立即上抛

**影响文件：**
- `core/context_compression/layer2.py` — 追加

**依赖任务：** T8, T10

**参考资料：**
- `PTL_RETRY_LIMIT = 3`，`PTL_DROP_PERCENTAGE = 0.2`
- 摘要请求不传 tools、不更新 usage_anchor

---

## T12: run_summary + auto_compact + force_compact

**说明：** 在 `layer2.py` 中追加：
- `async run_summary(in_: ManageInput) → list[Message]`：入口拍 recovery 快照 → summarize_once（遇 PTL 走 ptl_retry） → build_recovery_attachment → pick_recent_tail → _join_after_summary → 返回拼接后列表
- `async auto_compact(in_: ManageInput) → tuple[list[Message], int, int]`：调 run_summary；成功 → record_success + 返回 new_msgs；失败 → record_failure + raise
- `async force_compact(in_: ManageInput) → tuple[list[Message], int, int]`：同 auto_compact 但不调 auto_tracking 任何方法

**影响文件：**
- `core/context_compression/layer2.py` — 追加

**依赖任务：** T9, T11

**参考资料：**
- spec_compress.md 第二层流程

---

## T13: manage_context 编排入口

**说明：** 创建 `compact.py`：
- `class TriggerKind(Enum)`：AUTO / MANUAL / EMERGENCY
- `@dataclass ManageInput` / `@dataclass ManageOutput`
- `async manage_context(in_: ManageInput) → ManageOutput`：
  - MANUAL：跳过 layer1 + 阈值 + 熔断，直接 force_compact
  - EMERGENCY：先强制 offload_and_snip → 再 force_compact
  - AUTO：offload_and_snip → 重估 token → context_window 下界 sanity check（≤ 33,000 则 skip + warning） → 阈值判断 → auto_compact（若未熔断）

**影响文件：**
- `core/context_compression/compact.py` — 新建
- `core/context_compression/__init__.py` — 重导出

**依赖任务：** T7, T12

**参考资料：**
- spec_compress.md manage_context 流程
- `SUMMARY_RESERVE + AUTO_SAFETY_MARGIN = 33000`

---

## T14: PromptTooLongError 哨兵异常

**说明：**
- `llm/__init__.py` 新增 `class PromptTooLongError(Exception): pass`
- `llm/anthropic_client.py`：`stream_chat()` 中捕获 400 错误且 message 含 "prompt is too long" 时，`wrapped = PromptTooLongError(...); wrapped.__cause__ = orig`，通过 `yield StreamError(...)` 投递（要求上层 `isinstance(err, PromptTooLongError)` 可命中）——若当前架构 StreamError 不支持 cause 链，改为 `raise PromptTooLongError(...) from orig` 直接抛出
- `llm/openai_client.py`：同上，捕获 `context_length_exceeded` 错误码

**影响文件：**
- `llm/__init__.py` — 修改
- `llm/anthropic_client.py` — 修改
- `llm/openai_client.py` — 修改

**依赖任务：** 无

**参考资料：**
- Anthropic API error: `BadRequestError` + message "prompt is too long"
- OpenAI API error: `BadRequestError` + code "context_length_exceeded"

---

## T15: ConversationManager.replace_history

**说明：** 修改 `ConversationManager`：
- 新增 `replace_history(self, msgs: list[Message]) → None`：深拷贝 `copy.deepcopy(msgs)` 后直接替换 `self._messages`
- 现有 `add_xxx` / `messages` / `to_api_format` 等方法无需加锁（asyncio 单线程保证串行）

**影响文件：**
- `conversation/manager.py` — 修改

**依赖任务：** 无

**参考资料：**
- `copy.deepcopy`（CPython 3.12 下毫秒级）

---

## T16: ProviderConfig 新增 context_window

**说明：**
- `config/model.py`：`ProviderConfig` 末尾追加 `context_window: int = 0` 字段
- `config/protocol_defaults.py`（新建）：`DEFAULT_ANTHROPIC_CONTEXT_WINDOW = 200000` / `DEFAULT_OPENAI_CONTEXT_WINDOW = 128000`
- `config/loader.py`：加载时映射 `context_window = raw.get("context_window", 0)`

**影响文件：**
- `config/model.py` — 修改
- `config/protocol_defaults.py` — 新建
- `config/loader.py` — 修改

**依赖任务：** 无

**参考资料：**
- spec_compress.md Out of Scope：仅 `context_window` 可配

---

## T17: SessionRuntime 定义

**说明：** 创建 `core/agent/runtime.py`：
```python
@dataclass
class SessionRuntime:
    replacement: ContentReplacementState
    recovery: RecoveryState
    auto_tracking: CompactCircuitBreaker
    session: SessionContext
    context_window: int = 200000
    usage_anchor: int = 0
    anchor_msg_len: int = 0
```

**影响文件：**
- `core/agent/runtime.py` — 新建

**依赖任务：** T2, T3, T4

**参考资料：**
- spec_compress.md SessionRuntime 定义

---

## T18: Agent 集成 — 主循环

**说明：** 修改 `Agent.__init__()` 接受 `runtime: SessionRuntime | None`；`run()` 主循环每轮构建消息后、`stream_chat()` 前：
1. 按 mode 选 `tool_defs`
2. 构造 `ManageInput(trigger=AUTO)`，调 `manage_context()`
3. `stream_chat()` 完成后更新 `runtime.usage_anchor` 与 `anchor_msg_len`
4. 摘要请求不更新这两个字段
5. runtime is None 时退化（所有压缩不触发）

**影响文件：**
- `core/agent/agent.py` — 修改

**依赖任务：** T13, T15, T17

**参考资料：**
- `core/agent/agent.py` Agent.run() 现有主循环结构
- `llm/client.py` LLMClient.stream_chat() 签名

---

## T19: Agent 集成 — ReadFile 追踪

**说明：** 在 Agent 的工具执行阶段，对 ReadFile 调用：
- 从 `ToolUse.input` 取 `path` 参数
- `asyncio.to_thread(Path(abs_path).read_bytes)` 读纯净字节
- `runtime.recovery.record_file(abs_path, data.decode("utf-8", errors="replace"))`
- 读盘失败 `try/except OSError: pass`
- 调用必须在 `add_tool_result()` 之前（同 task 顺序）

**影响文件：**
- `core/agent/agent.py` — 修改

**依赖任务：** T4, T18

**参考资料：**
- `core/tool/tools/read_file.py` 现有参数名
- spec_compress.md ReadFile 追踪要求

---

## T20: Agent 集成 — 紧急压缩

**说明：** 在 Agent 主循环中捕获 `stream_chat()` 的异常：
- `isinstance(err, PromptTooLongError)` 且 `emergency_retried == False` → 构造 `ManageInput(trigger=EMERGENCY)` → `manage_context()` → `usage_anchor=0, anchor_msg_len=0` → 重新估算 → 若 `est < window - 3000` 则重试一次 stream；否则上抛
- `emergency_retried` 为迭代级局部变量，锁定一次性重试

**影响文件：**
- `core/agent/agent.py` — 修改

**依赖任务：** T14, T18

**参考资料：**
- spec_compress.md 紧急压缩路径
- `MANUAL_SAFETY_MARGIN = 3000`

---

## T21: Agent 集成 — run_force_compact

**说明：** 新增 `Agent.run_force_compact(conv, tool_defs) → tuple[int, int]`：
- 构造 `ManageInput(trigger=MANUAL)`，调 `manage_context()`
- 返回 `(before_tokens, after_tokens)`
- 失败抛异常由 TUI 捕获

**影响文件：**
- `core/agent/agent.py` — 修改

**依赖任务：** T18

**参考资料：**
- spec_compress.md 手动压缩路径

---

## T22: Compact 状态事件

**说明：**
- `core/agent/event.py` 或 `core/agent/events.py`：新增 `CompactPhase` 枚举 + `CompactEvent` dataclass；现有 AgentEvent 联合类型追加 `CompactEvent`
- Agent 主循环中 emit：自动路径触发前 `BEFORE_AUTO`、完成后 `AFTER_AUTO`；紧急路径 `BEFORE_EMERGENCY` / `AFTER_EMERGENCY`；layer1 不发事件

**影响文件：**
- `core/agent/events.py` — 修改
- `core/agent/agent.py` — 修改

**依赖任务：** T18, T20

**参考资料：**
- spec_compress.md Compact 状态事件

---

## T23: TUI 命令分发框架

**说明：** 创建 `tui/commands.py`：
- `CommandHandler = Callable[["CodeForgeApp"], Awaitable[None]]`
- `BUILTIN_COMMANDS` 字典：`/exit`、`/plan`、`/do`、`/compress`
- `dispatch_command(input_: str) → tuple[CommandHandler | None, bool]`
- `handle_compress(app)`：在 asyncio.create_task 里调 `app.agent.run_force_compact()`，完成后回投系统消息 `已压缩，token 从 X 降至 Y` 或 `压缩失败: <err>`
- 命令路径不调 `conv.add_user`，命令输入不进入对话历史

**影响文件：**
- `tui/commands.py` — 新建

**依赖任务：** T21

**参考资料：**
- 现有 TUI 斜杠命令处理逻辑（`tui/app.py`）

---

## T24: TUI 接入 — 持有 SessionRuntime + 命令分发

**说明：**
- `tui/app.py`：`CodeForgeApp` 新增 `runtime: SessionRuntime` 与 `agent: Agent` 字段
- 启动期构造 `SessionRuntime` 与 `Agent`（不再每轮重新构造 Agent）
- stream 处理中检测 `CompactEvent`，渲染系统消息（`正在压缩上下文...` / `已压缩，token 从 X 降至 Y` / `压缩失败: <err>` 等）
- 输入路径接入 `dispatch_command`

**影响文件：**
- `tui/app.py` — 修改
- `tui/renderer.py` — 可能需要新增 compact 状态消息渲染

**依赖任务：** T22, T23

**参考资料：**
- spec_compress.md Compact 状态事件文案

---

## T25: 单元测试

**说明：** 创建 `tests/test_context_compression.py`（或按模块拆多个 test 文件）：
- state：决策冻结（kept/replaced/skip）、熔断计数、snapshot 排序
- token：锚点估算、字符增量、usage 合并
- layer1：单条落盘、聚合落盘、幂等性、落盘失败降级、预览体稳定性
- summary_prompt：结构断言（9 标题 + analysis/summary 标签 + 禁止工具调用）、extract_summary 三种 case
- recovery：文件上限（5 条）、单文件截断、工具列表匹配、边界提示稳定性
- layer2：近期原文边界、配对修正、PTL 重试序列、按比例丢弃
- compact：自动触发/跳过、熔断、手动绕过、紧急路径

**影响文件：**
- `tests/test_context_compression.py` — 新建

**依赖任务：** T13

**参考资料：**
- spec_compress.md 验收标准
- 现有 test 模式 `tests/test_message.py`

---

## T26: 端到端验证

**说明：**
- `pytest tests/ -v` 全量通过（不破坏现有测试）
- 模拟长对话自动触发 layer2
- 模拟单条大工具结果触发 layer1
- 模拟紧急压缩路径
- 模拟 `/compress` 命令
- `ruff check .` 无告警；`ruff format --check .` 通过

**影响文件：**
- 无新文件（验证用 pytest 运行全部测试）

**依赖任务：** T25（全部集成）

**参考资料：**
- checklist_compress.md 验收项
