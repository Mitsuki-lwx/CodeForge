# CodeForge 上下文压缩 · 规格说明

## 背景

CodeForge 的 Token 消耗大头是工具结果（读文件、grep、bash 等），单次工具调用可能返回数十万字符。当前不做任何处理直接全量喂给 LLM，导致上下文快速膨胀。同时，用户的原始消息应尽量原文保留，不能被任何形式的摘要改写。

需要两层的上下文压缩机制，每次 API 请求前按顺序执行：

1. **第一层预防**（轻量，无网络开销）：单条工具结果超阈值时完整内容写磁盘，对话中只留预览和文件路径；同时控制单条消息内所有工具结果的合计大小
2. **第二层兜底**（昂贵，调 LLM）：整体对话逼近窗口上限时，调 LLM 生成结构化摘要替换旧消息

## 目标用户

CodeForge 终端用户，在长对话中自动受益于上下文压缩，也可手动 `/compress` 主动触发。

## 架构原则

- **依赖方向无环**：`core.context_compression` 不 import `core.agent`、`config`、`tui`。Agent 依赖 compact，TUI 依赖 compact，config 仅向外部提供 `effective_context_window()`。
- **长生命周期状态外置**：压缩决策账本、文件追踪、熔断计数、usage_anchor 等跨轮状态放在 `SessionRuntime` 中，由 TUI Model 持有，每轮注入 Agent。Agent 不再每轮重新构造。
- **决策冻结**：一旦某个 tool_use_id 被决定替换，全会话内不得改变该决策（prompt cache 稳定）。
- **摘要请求不传 tools**：防止模型在摘要阶段发起工具调用。
- **摘要不更新 usage_anchor**：只有主对话路径的 stream 完成才更新锚点。

## 能力清单

1. **单工具结果落盘** — 单个 tool_result 超过 50,000 字符时，完整内容写入 `.codeforge/sessions/<session_id>/tool-results/<tool_use_id>`，对话中替换为预览体（原始字节数 + 头部预览 + 落盘路径 + 重读提示）
2. **单消息合计落盘** — 同一条 role=tool 消息内所有 tool_result 合计超过 200,000 字符时，按体积倒序依次落盘，直到合计降至阈值以下
3. **决策冻结** — 替换决策通过 `ContentReplacementState.decide_once` 原子完成（查账本→决策→落盘→写账本），同一 tool_use_id 全会话决策不变，保证 prompt cache 逐字节稳定
4. **落盘失败降级** — 磁盘写入失败时该条不替换、不写账本，下一轮重试；不阻断主流程
5. **结构化摘要压缩** — 总 token 估算超过 `context_window - 20000 - 13000` 时，调 LLM 生成 9 分区结构化摘要，含 `<analysis>` 草稿 + `<summary>` 正式摘要两阶段，草稿用完即弃
6. **用户原话保护** — 摘要第 6 分区原文保留所有用户消息。压缩后追加三段恢复内容（最近读过的文件快照 + 当前可用工具列表 + 边界提示），边界提示明确告知模型需要文件细节时用 ReadFile 重新读取
7. **禁止工具调用** — 摘要 Prompt 首尾各强调一次禁止 LLM 调用任何工具；摘要请求 `tools=None`
8. **近期原文保留** — 摘要后保留尾部近期消息，同时满足累计 token ≥ 10,000 且条数 ≥ 5（两个下界都满足后停手，择宽保留）；再做 tool_use/tool_result 配对修正，保证不切断调用链
9. **锚定 token 估算** — `estimate_tokens = usage_anchor + ceil(anchor 之后新增消息字符数 / 3.5)`，锚点由上一次主对话 stream 真实 usage 提供，避免重复计算历史
10. **PTL 自重试** — 摘要请求自身撞上下文上限（PromptTooLongError）时，按"用户提交 + 一组 assistant/tool 往返"分组，前 3 次每次丢最旧 1 组重试，之后按剩余组数 × 20% 丢弃（至少 1 组），直到成功或全部丢光
11. **紧急压缩** — 主对话 stream 返回 PromptTooLongError 时，先强制跑一次 layer1 把大工具结果挪走，再跑 force_compact 重建对话历史，成功后重置锚点、主对话重试一次；失败不重试第三次
12. **熔断器** — 自动压缩连续失败 3 次后跳闸，跳闸后跳过自动 layer2（避免死循环）；手动和紧急路径不受熔断影响
13. **手动 `/compress`** — TUI 命令分发框架注册 `/compress`，调 Agent.run_force_compact()，无视阈值和熔断；完成后显示 token 变化
14. **恢复三段** — 摘要消息合并为一条 user 消息，内含：9 部分摘要 + 最近读过的文件快照（≤5 个，时间戳倒序，单文件 ≤5000 token 头部保留） + 当前可用工具列表 + 边界提示
15. **角色衔接修正** — 摘要 user 消息后若近期原文首条也是 user，插入一条 assistant 衔接占位消息，保证 Anthropic user/assistant 交替约束
16. **Compact 状态事件** — 自动压缩触发前 emit `BEFORE_AUTO` 事件，完成后 emit `AFTER_AUTO` 事件（带 before/after token 数）；紧急压缩 emit `BEFORE_EMERGENCY` / `AFTER_EMERGENCY`；layer1 不发事件（静默）
17. **会话目录管理** — 进程启动生成 `session_id = <unix_ts>-<hex>`，落盘目录 `.codeforge/sessions/<session_id>/tool-results/`；会话结束不自动清理（留作调试副产物），`.gitignore` 追加 `.codeforge/sessions/`
18. **ReadFile 追踪** — Agent 在 ReadFile 工具成功后，用 `asyncio.to_thread` 重读磁盘纯净字节，写入 `RecoveryState`，作为恢复段的文件快照来源

## 非功能要求

- **不阻塞请求** — layer1 的字符串检查 + 文件写入在消息构建管道中同步完成，零网络开销
- **不会话泄漏** — 进程退出后 session 目录保留但不影响下次启动
- **不破坏角色交替** — 压缩后的消息序列经 `check_alternating()` 校验通过
- **不破坏现有测试** — `pytest tests/ -v` 全量通过
- **Token 估算轻量** — 使用 `字符数 ÷ 3.5` 估算 token 数，不依赖外部 tokenizer
- **决策账本线程安全** — asyncio 单线程事件循环保证串行，无需显式锁
- **context_window 下界检查** — 必须 > 33,000（SUMMARY_RESERVE + AUTO_SAFETY_MARGIN），低于此值跳过自动 layer2 并 warning

## 设计骨架

```
core/context_compression/
├── __init__.py              # 重导出 manage_context / TriggerKind / State 类型
├── const.py                 # 全部硬编码常量
├── state.py                 # ContentReplacementState / CompactCircuitBreaker / RecoveryState / SessionContext
├── token.py                 # estimate_tokens / usage_anchor / message_chars
├── layer1.py                # offload_and_snip / spill_single / build_preview
├── summary_prompt.py        # build_summary_prompt / serialize_conversation / extract_summary
├── recovery.py              # build_recovery_attachment / render_file_block / render_tools_block / BOUNDARY_NOTICE
├── layer2.py                # auto_compact / force_compact / run_summary / summarize_once / ptl_retry / pick_recent_tail / group_by_user_turn
└── compact.py               # manage_context 主入口 / TriggerKind / ManageInput / ManageOutput

core/agent/runtime.py        # (新建) SessionRuntime dataclass
core/agent/event.py          # (修改) Event 追加 compact: CompactEvent | None
core/agent/agent.py          # (修改) 主循环集成 manage_context / ReadFile 追踪 / 紧急压缩 / run_force_compact
conversation/manager.py      # (修改) 新增 replace_history() 深拷贝整体替换
llm/__init__.py              # (修改) 新增 PromptTooLongError 哨兵异常
llm/anthropic_client.py      # (修改) 捕获上下文过长异常 → 包装为 PromptTooLongError
llm/openai_client.py         # (修改) 同上
tui/commands.py              # (新建) 命令分发框架 + /compress / /exit / /plan / /do
tui/app.py                   # (修改) 持有 SessionRuntime 与 Agent 引用
```

### 模块依赖关系

```
compact 子包（不 import agent / config / tui）
    ↑
agent（依赖 compact）
    ↑
tui（依赖 agent + compact）
```

`config/protocol_defaults.py` 定义协议默认窗口值，由 cli 启动时注入 `SessionRuntime.context_window`，compact 通过 `ManageInput.context_window` 接收，不反向 import config。

### 两层架构流程

```
每次 API 请求前（Agent 主循环）：

  ┌─────────────────────────────────────────────────┐
  │ 第一层：预防（轻量，零网络开销）                   │
  │                                                 │
  │ offload_and_snip(msgs, replacement, session):    │
  │   for each role=tool 消息:                       │
  │     对每个未决策 tool_result:                     │
  │       单条 > 50K chars? → spill_single + 替换     │
  │       聚合 > 200K chars? → 选大依次 spill          │
  │     通过 decide_once 原子写账本                   │
  │   → 返回处理后的 msgs                            │
  │   → conv.replace_history(layer1_out)             │
  └─────────────────────────────────────────────────┘
                    ↓
  estimate_tokens(anchor, layer1_out, anchor_msg_len)
                    ↓
  ┌─────────────────────────────────────────────────┐
  │ 第二层：兜底（昂贵，调 LLM）                      │
  │                                                 │
  │ 若 est >= window - 20000 - 13000 且未熔断:       │
  │   auto_compact():                                │
  │     a. recovery.snapshot() 拍快照                │
  │     b. summarize_once → 解析 <summary>           │
  │        (若 PTL → ptl_retry 逐组丢弃重试)          │
  │     c. build_recovery_attachment(快照, tools)     │
  │     d. pick_recent_tail + 配对修正               │
  │     e. _join_after_summary 拼接 + role 衔接修正  │
  │     f. conv.replace_history(new_msgs)            │
  │     成功 → 熔断清零；失败 → 熔断 +1              │
  └─────────────────────────────────────────────────┘
                    ↓
              发送 API 请求
```

### 紧急压缩路径

```
_stream_once 返回 err → isinstance(err, PromptTooLongError)
    ↓
emergency_retried 已为 True? → 是: 上抛异常
    ↓ 否
manage_context(trigger=EMERGENCY):
  1. offload_and_snip 强制跑一次（挪走大工具结果）
  2. force_compact → run_summary → replace_history
     (内部如 PTL 走 ptl_retry，不调熔断器)
    ↓
usage_anchor=0, anchor_msg_len=0, 重新估算
    ↓
est < window - 3000? → 是: 重试主对话 stream
                     → 否: 上抛异常（不可恢复）
```

### 手动压缩路径

```
TUI /compress → dispatch_command → handle_compact
    ↓
agent.run_force_compact(conv, tool_defs)
    ↓
manage_context(trigger=MANUAL):
  跳过 layer1、阈值检查、熔断器
  直接 force_compact → run_summary → replace_history
    ↓
返回 (before_tokens, after_tokens)
TUI 显示 "已压缩，token 从 X 降至 Y"
```

### 核心常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `SINGLE_RESULT_LIMIT` | 50,000 | 单条工具结果落盘阈值（字符） |
| `MESSAGE_AGGREGATE_LIMIT` | 200,000 | 单条消息内工具结果聚合阈值（字符） |
| `SUMMARY_RESERVE` | 20,000 | 给摘要 LLM 输出预留的 token |
| `AUTO_SAFETY_MARGIN` | 13,000 | 自动触发的额外安全余量 |
| `MANUAL_SAFETY_MARGIN` | 3,000 | 手动/紧急触发安全余量 |
| `RECOVERY_FILE_LIMIT` | 5 | 恢复段最多展示文件数 |
| `RECOVERY_TOKENS_PER_FILE` | 5,000 | 单文件快照 token 上限 |
| `RECENT_KEEP_TOKENS` | 10,000 | 近期原文保留 token 下界 |
| `RECENT_KEEP_MESSAGES` | 5 | 近期原文保留条数下界 |
| `MAX_CONSECUTIVE_AUTO_COMPACT_FAILURES` | 3 | 熔断阈值 |
| `PTL_RETRY_LIMIT` | 3 | 摘要 PTL 直接重试次数 |
| `PTL_DROP_PERCENTAGE` | 0.2 | 3 次后每次丢弃比例 |
| `ESTIMATE_CHARS_PER_TOKEN` | 3.5 | 字符/token 估算比 |
| `PREVIEW_HEAD_BYTES` | 2,048 | 预览体头部字节上限 |
| `PREVIEW_HEAD_LINES` | 20 | 预览体头部行数上限 |

### 关键类型

**ContentReplacementState**（决策账本）：
```python
class ContentReplacementState:
    _seen_ids: set[str]          # 已决策的 tool_use_id（无论 kept 还是 replaced）
    _replacements: dict[str, str] # 只存 replaced 的预览字符串

    def decide_once(
        self,
        tool_use_id: str,
        original: str,
        decide: Callable[[], tuple[str, str]],  # → ("kept"|"replaced"|"skip", preview)
    ) -> str:
        """原子完成"查账本→决策→写账本"。已 Seen 的 id 直接返回存量结果。"""
```

**RecoveryState**（文件追踪）：
```python
@dataclass
class FileReadRecord:
    path: str
    content: str        # 不带行号前缀的纯净字节
    timestamp: datetime

class RecoveryState:
    _files: dict[str, FileReadRecord]  # 键为绝对路径

    def record_file(self, path: str, content: str) -> None: ...
    def snapshot(self) -> list[FileReadRecord]:  # 时间戳倒序拷贝
```

**SessionRuntime**（跨轮长生命周期状态）：
```python
@dataclass
class SessionRuntime:
    replacement: ContentReplacementState
    recovery: RecoveryState
    auto_tracking: CompactCircuitBreaker
    session: SessionContext
    context_window: int = 200000
    usage_anchor: int = 0       # 主对话路径真实 usage 之和；摘要不更新
    anchor_msg_len: int = 0     # anchor 当时 conv 消息条数
```

**ManageInput / ManageOutput**：
```python
@dataclass
class ManageInput:
    conv: ConversationManager
    provider_config: ProviderConfig
    model: str
    context_window: int
    tool_defs: list[dict]
    replacement: ContentReplacementState
    recovery: RecoveryState
    auto_tracking: CompactCircuitBreaker
    session: SessionContext
    usage_anchor: int
    anchor_msg_len: int
    estimated_token: int
    trigger: TriggerKind

@dataclass
class ManageOutput:
    before_tokens: int
    after_tokens: int
```

### 摘要 Prompt 结构

```
你必须不调用任何工具。你的任务是对以上对话生成结构化摘要。

第一步：输出 <analysis>...</analysis>，分析对话内容，确保覆盖所有 9 个分区。
第二步：基于分析，输出 <summary>...</summary>，正式摘要约 5000 汉字。

<summary> 必须包含以下 9 个固定小节：
1. 主要请求和意图 — 用户到底想做什么
2. 关键技术概念 — 讨论过的重要技术点
3. 文件和代码段 — 涉及哪些文件，关键代码片段要保留
4. 错误和修复 — 遇到了什么错，怎么解决的
5. 问题解决过程 — 解决问题的思路和方法
6. 所有用户消息 — 用户说过的所有非工具结果的话（原文保留！）
7. 待办任务 — 还没完成的事
8. 当前工作 — 最近在做什么（要最详细）
9. 可能的下一步 — 接下来打算做什么

你必须不调用任何工具。输出纯文本。
```

### 恢复三段结构

摘要消息 content = 9 部分摘要 + 以下三段：

1. **最近读过的文件** — 取快照前 5 个，时间戳倒序；单文件 > 5,000 token 时保留头部、截掉尾部并追加 `(content truncated)`
2. **当前可用工具** — 每行一个工具名 + 描述 + input_schema 紧凑 JSON
3. **边界提示** — 固定文案：需要文件原文/错误原文/用户原话时请用文件读取工具重新读取，不要依据摘要内容做猜测

### 消息拼接规则

- 摘要 + 恢复三段合并到**同一条** user 消息（避免 user/user 连续违反 Anthropic 协议）
- 近期原文通过 `_join_after_summary` 拼在摘要消息后：若近期原文首条也是 user，中间插入 assistant 衔接占位
- `pick_recent_tail` 从尾到头累加，token ≥ 10,000 且条数 ≥ 5 后停手（两个下界都满足），再做 tool_use/tool_result 配对修正

## Out of Scope

- **精确 token 计数** — 使用字符数 ÷ 3.5 估算，不上 tiktoken 或 token 计数 API
- **分块压缩** — 摘要只做一份，不按时间分多段
- **摘要质量自动评估** — 不检查摘要是否覆盖了所有关键信息
- **压缩策略配置化** — 阈值、分区数等不通过配置文件调整（仅 `context_window` 可配）
- **摘要缓存复用** — 每次触发都重新生成摘要
- **增量摘要** — 不支持在已有摘要基础上追加新压缩
- **流式摘要** — 摘要生成走完整 LLM 调用，不走流式
- **MCP 工具运行时增删** — run 期间 ToolRegistry 不可变，MCP 工具注册/注销只在 run 之间
- **会话恢复** — 进程重启后 session 目录保留但状态不恢复，重新开始
- **运行期切 provider** — `context_window` 在 provider 选定后注入，会话内不变
