# Checklist — 上下文压缩

每一项通过运行代码或观察行为来验证。条目格式：操作 → 预期可观测结果。

---

## 实现完整性

### 包与目录结构

- [ ] `ls core/context_compression/` 列出 const.py / state.py / token.py / layer1.py / summary_prompt.py / recovery.py / layer2.py / compact.py / __init__.py 九个文件
- [ ] `python -c "from core.context_compression import manage_context, TriggerKind"` 退出码 0
- [ ] `grep -rn "= 50000\|= 200000\|= 20000\|= 13000\|= 3000\|= 10000" core/context_compression/` 全部命中在 const.py

### 状态对象

- [ ] `new_session_context(".")` 返回 `session_id` 形如 `<unix_ts>-<hex>`，`spill_dir` 目录物理存在
- [ ] 连续两次 `new_session_context` 得到不同 `session_id`
- [ ] `ContentReplacementState.decide_once(id, orig, lambda: ("kept", ""))` → 返回 orig；再次调用不再调回调
- [ ] `decide_once(id, orig, lambda: ("replaced", "PREVIEW"))` → 返回 "PREVIEW"；再次调用返回同一份 "PREVIEW"（逐字节相等），不重新调回调
- [ ] `decide_once(id, orig, lambda: ("skip", ""))` → 返回 orig；账本未写入；再次调用仍走回调
- [ ] `CompactCircuitBreaker` 连续 3 次 `record_failure()` → `tripped() == True`
- [ ] `record_success()` 后 `tripped() == False`（清零）
- [ ] `RecoveryState.record_file()` + `snapshot()` 返回时间戳倒序列表
- [ ] 修改 `snapshot()` 返回的列表不影响下次调用结果
- [ ] 50 个线程并发 `record_file` + `snapshot` → 无异常（覆盖并发安全）

### Token 估算

- [ ] `estimate_tokens(0, [], 0)` 返回 0
- [ ] `estimate_tokens(5000, [msg], 0)` 其中 msg.content 350 字符 → 返回 `5000 + ceil(350/3.5) = 5100`
- [ ] `estimate_tokens(5000, [m1, m2], 1)` 只算 m2 的字符增量（anchor_msg_len=1 跳过 m1）
- [ ] `usage_anchor({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 30, "cache_creation_input_tokens": 20})` 返回 200

### 第一层 Layer1

- [ ] `spill_single(session, "abc123", "hello")` 写入文件；第二次调用幂等不重写（st_mtime_ns 不变）
- [ ] `offload_and_snip` 对 60,000 字符单条工具结果 → 被替换为预览体
- [ ] 预览体包含 4 个稳定标志：`original size:`、落盘路径片段、`head preview`、`文件读取工具` 和 `不要凭头部预览猜测`
- [ ] 预览体头部 ≤ 20 行且 ≤ 2,048 字节
- [ ] 同入参连续两次 `build_preview` → 逐字节相等
- [ ] 3 条 80,000 字符工具结果 → 至少 2 条被替换，聚合回落到 ≤ 200,000
- [ ] 同一 id 跑两次 `offload_and_snip` → 第二次输出与第一次逐字节一致（决策冻结）
- [ ] spill_dir 不可写（`chmod 0o500`）→ 落盘失败，对应结果保持原文，账本未写入

### 摘要 Prompt

- [ ] `build_summary_prompt(msgs)` 返回列表长度为 1，单条 user 消息
- [ ] Prompt 以 "你必须不调用任何工具" 开头
- [ ] Prompt 以 "你必须不调用任何工具" 结尾
- [ ] Prompt 包含 `<analysis>` 和 `<summary>` 标签说明
- [ ] Prompt 包含 9 个小节标题（字面字符串匹配）
- [ ] 相同 msgs 两次 `serialize_conversation` → 逐字节相等
- [ ] `extract_summary("abc<summary>hello</summary>yy")` 返回 "hello"
- [ ] `extract_summary("no tags here")` 返回 "no tags here"（降级）

### 恢复三段

- [ ] `build_recovery_attachment(snapshot, tool_defs)` 输出包含 `最近读过的文件` / `当前可用工具` / `边界提示` 三个标题
- [ ] 7 条文件记录 → 输出仅含最近 5 条；第 6、第 7 条路径**不**出现（反向断言 `not in`）
- [ ] 超长 content → 保留头部，尾部出现 `(content truncated)`
- [ ] 工具列表逐项匹配：set(输出工具名) == set(tool_defs 工具名)
- [ ] 相同 snapshot + tool_defs 连续两次调用 → 输出逐字节相等（边界提示稳定性）

### 第二层 Layer2

- [ ] `pick_recent_tail` token < 10,000 或条数 < 5 → 返回全部
- [ ] `pick_recent_tail` 两个下界都满足后停手；返回值 token ≥ 10,000 且条数 ≥ 5
- [ ] 截断点落在 tool_result → 自动前推到配对 tool_use 之前
- [ ] `_join_after_summary` recent 首条为 user → 插入 assistant 衔接占位
- [ ] `group_by_user_turn([u, a, t, u, a])` 返回 2 组，第 0 组 3 条、第 1 组 2 条
- [ ] `summarize_once` 请求 body 中 tools 为 None / 空
- [ ] `ptl_retry` 前 3 次每次丢最旧 1 组；第 4 次成功
- [ ] `ptl_retry` 超过 3 次后按 `ceil(剩余 × 0.2)` 丢（至少 1 组）
- [ ] `ptl_retry` groups 全部丢光 → 抛最后异常，不发送 messages 为空的请求
- [ ] `run_summary` 返回列表首条为 user 消息（摘要 + 恢复合并）
- [ ] `auto_compact` 成功 → `auto_tracking._consecutive_failures == 0`
- [ ] `auto_compact` 失败 → `auto_tracking._consecutive_failures += 1`
- [ ] `force_compact` 失败不调 auto_tracking 任何方法

### manage_context 编排

- [ ] `trigger=AUTO` + `estimated_token < threshold` → 仅执行 layer1，不触发 layer2
- [ ] `trigger=AUTO` + `estimated_token >= threshold` → 执行 layer1 + layer2
- [ ] `trigger=AUTO` + `context_window <= 33000` → 跳过自动 layer2 + logging.warning
- [ ] `trigger=AUTO` + 熔断器 tripped → 跳过 layer2
- [ ] `trigger=MANUAL` + estimated_token 远低于阈值 → 仍执行 layer2
- [ ] `trigger=EMERGENCY` → 先 layer1 再 force_compact
- [ ] layer1 之后重估算 token（用 layer1_out 而非 in_.estimated_token）

### PromptTooLongError

- [ ] `grep -n "class PromptTooLongError" llm/__init__.py` 命中
- [ ] Anthropic provider 400 错误含 "prompt is too long" → 抛出 `PromptTooLongError`
- [ ] OpenAI provider `context_length_exceeded` → 抛出 `PromptTooLongError`
- [ ] 其他 4xx/5xx 错误不被错误包装为 PTL

### ConversationManager.replace_history

- [ ] `replace_history(msgs)` 后修改入参 msgs → `messages()` 不被影响（深拷贝）
- [ ] `replace_history(None)` 不抛异常（防御性 `msgs = msgs or []`）
- [ ] `replace_history([])` 后 `messages()` 长度为 0

### Config

- [ ] `ProviderConfig(context_window=0)` + protocol=anthropic → `effective_context_window()` 返回 200000
- [ ] `ProviderConfig(context_window=0)` + protocol=openai → 返回 128000
- [ ] `ProviderConfig(context_window=80000)` → 返回 80000
- [ ] 未知 protocol + 未配置 → 返回 200000（保守默认）

---

## 集成

### Agent 主循环

- [ ] Agent 构造 `runtime=None` 时退化——不触发任何压缩，主循环正常执行
- [ ] 每轮 `stream_chat()` 前调 `manage_context(trigger=AUTO)`
- [ ] 主对话 stream 完成后 usage_anchor 被**替换**为最新值（连续 3 次不同 Usage 依次 1000/1500/2200 → anchor 依次为这些值）
- [ ] 摘要请求（layer2 路径）结束后 usage_anchor **不**被修改

### Agent ReadFile 追踪

- [ ] ReadFile 成功后 `recovery.snapshot()` 包含该文件路径
- [ ] 记录内容不含行号前缀（与磁盘原文逐字节相等）
- [ ] 读盘失败（文件不存在）→ `try/except OSError: pass`，不阻断主循环
- [ ] 其他工具调用不触发 record_file
- [ ] record_file 在 `add_tool_result()` 之前完成

### Agent 紧急压缩

- [ ] 第 1 次 stream 返回 PromptTooLongError → 紧急压缩 → 重试 stream 成功
- [ ] 紧急压缩后重试再次 PTL → Agent 上抛异常，不进入第三次
- [ ] 紧急压缩后重新估算 token ≥ `window - 3000` → 直接上抛异常（不可恢复）

### Agent Compact 状态事件

- [ ] 自动压缩触发前 emit `BEFORE_AUTO`，完成后 emit `AFTER_AUTO`（before > after, err=None）
- [ ] 估算 token 低于阈值 → 不 emit 任何 Compact 事件
- [ ] 紧急压缩 emit `BEFORE_EMERGENCY` → `AFTER_EMERGENCY` 一对事件
- [ ] `AFTER_EMERGENCY` 失败时 `err is not None`

### TUI 命令分发

- [ ] `grep -n "/compress\|/exit\|/plan\|/do" tui/commands.py` 全部命中
- [ ] 输入 `/compress` → 调 `agent.run_force_compact()`，不调 LLM 主对话路径
- [ ] `/compress` 成功 → TUI 显示 `已压缩，token 从 X 降至 Y`（X、Y 为非负整数）
- [ ] `/compress` 失败 → TUI 显示 `压缩失败: <err>`
- [ ] 输入 `/unknown` → 友好提示含可用命令列表，不发 LLM
- [ ] `/exit` / `/plan` / `/do` 迁移后行为不变

### TUI Compact 事件渲染

- [ ] `BEFORE_AUTO` → scrollback 出现 `正在压缩上下文...`
- [ ] `BEFORE_EMERGENCY` → scrollback 出现 `上下文撞墙，自动压缩中...`
- [ ] `AFTER_*` 成功 → scrollback 出现 `已压缩，token 从 X 降至 Y`
- [ ] `AFTER_*` 失败 → scrollback 出现 `压缩失败: ...`
- [ ] 手动 `/compress` 完成后的系统消息文本与 `format_compact_notice` 输出逐字节相同（统一格式化）

### Session 目录

- [ ] 进程启动后 `.codeforge/sessions/<id>/tool-results/` 目录存在
- [ ] session_id 格式：`<unix_ts>-<hex>`
- [ ] `.gitignore` 包含 `.codeforge/sessions/`
- [ ] 进程退出后目录仍保留，下次启动开新子目录

---

## 编译与测试

- [ ] `python -c "import core.context_compression"` 退出码 0
- [ ] `ruff check core/context_compression/` 无告警
- [ ] `ruff format --check core/context_compression/` 输出为空
- [ ] `ruff check --select I core/context_compression/` import 分组正确
- [ ] `pytest tests/test_context_compression.py -v` 全部通过
- [ ] `pytest tests/ -v` 全量通过（不破坏现有 130+ 测试）
- [ ] `grep -rn "参考\|取自\|对齐.*实现\|mirror\|镜像" core/context_compression/` 无命中

---

## 端到端场景

### E1: 单条大工具结果（Layer1 预防）

- [ ] **触发**：一轮工具调用返回 80,000 字节结果
- [ ] **预期**：
  - `ls .codeforge/sessions/<id>/tool-results/` 出现对应 tool_use_id 文件
  - 文件 size == 80,000 字节
  - 下一轮 stream 请求体中该 tool_result content 被替换为预览体
  - 预览体包含 `original size:` + 落盘路径 + `head preview` + 重读提示

### E2: 单轮聚合超标（Layer1 规则 B）

- [ ] **触发**：一条消息含 3 条 tool_result，每条 80,000 字节（合计 240,000）
- [ ] **预期**：
  - 至少 2 条被落盘替换
  - 下一轮请求中该消息剩余 tool_result content 字节合计 ≤ 200,000
  - spill_dir 至少出现 2 个文件

### E3: 决策冻结

- [ ] **触发**：同一 tool_use_id 在第 N 轮被决定替换
- [ ] **预期**：第 N+1 ~ N+5 轮该 tool_result 使用与第 N 轮逐字节相同的预览体（`==` 比较为 True）
- [ ] **触发**：另一 tool_use_id 在第 M 轮被决定保留
- [ ] **预期**：第 M+1 ~ M+5 轮该 tool_result 始终保持原文

### E4: 长会话自动压缩（Layer2）

- [ ] **触发**：30 轮迭代，每轮返回 30KB 工具结果，`context_window=50000`
- [ ] **预期**：
  - 30 轮完整跑完，无未捕获异常
  - 中途至少触发一次自动 layer2 摘要
  - `conv.messages()` 长度远小于 30
  - 摘要后首条消息为 user（含 9 部分摘要 + 恢复三段）
  - 第 6 部分包含所有 user 消息原文（逐条 `in` 命中）

### E5: 手动 /compress

- [ ] **触发**：在 TUI 输入 `/compress`，压缩前估算 token=1000（远低于自动阈值 167000）
- [ ] **预期**：
  - 调用了 layer2（无视阈值）
  - conversation 被替换为摘要 + 恢复 + 近期原文
  - TUI 显示 `已压缩，token 从 1000 降至 <新值>`
  - LLM 主对话路径未被调用

### E6: 紧急压缩（PTL 恢复）

- [ ] **触发 A**：stream 返回 PromptTooLongError → 紧急压缩成功 → 重试成功
- [ ] **预期 A**：整个过程成功完成；spill_dir 多了文件（紧急 layer1 生效）；usage_anchor 被清零
- [ ] **触发 B**：紧急压缩后重试再次 PTL
- [ ] **预期 B**：Agent 上抛异常，不第三次尝试
- [ ] **触发 C**：紧急压缩后 `est >= window - 3000`（不可恢复）
- [ ] **预期 C**：直接上抛异常，不发起第二次 stream

### E7: 熔断器

- [ ] **触发 A**：fake_provider 对摘要请求连续 3 次抛 500 → 第 4 次触发自动 layer2
- [ ] **预期 A**：第 4 次 layer2 不被触发（熔断生效）；`/compress` 仍能正常执行
- [ ] **触发 B**：摘要响应序列 [err, err, ok, err, err, err]
- [ ] **预期 B**：6 轮后才跳闸（第 3 个 ok 把计数清零），`_consecutive_failures` 序列 [1, 2, 0, 1, 2, 3]

### E8: 压缩后恢复信息完整性

- [ ] **触发**：压缩前先后读过 7 个不同文件
- [ ] **预期**：
  - 恢复段仅展示最近 5 个文件（时间戳倒序），第 6、第 7 个路径不出现（反向断言）
  - 工具列表与 `Request.tools` 逐项匹配
  - 第 6 部分含全部 user 消息原文
  - 边界提示固定文案完整

### E9: PTL 自重试

- [ ] **触发 A**：前 3 次摘要 PTL → 第 4 次成功
- [ ] **预期 A**：groups 数序列 [G, G-1, G-2, G-3] 且第 4 次成功
- [ ] **触发 B**：持续 PTL 直到 groups 全部丢光
- [ ] **预期 B**：抛最后异常；`auto_compact` 路径熔断 +1；`force_compact` 路径不调熔断

### E10: 多 provider context_window

- [ ] anthropic + 未配置 → Agent 拿到 200000，自动阈值 = 167000
- [ ] openai + 未配置 → Agent 拿到 128000，自动阈值 = 95000
- [ ] anthropic + 配置 100000 → Agent 拿到 100000，自动阈值 = 67000

### E11: 不切断 tool_use/tool_result 配对

- [ ] **触发**：对话尾部 [user, assistant(tool_use=A), tool(result A), assistant(tool_use=B), tool(result B)]
- [ ] **预期**：`pick_recent_tail` 返回列表第一条 role 不为 tool；若首条 assistant 带 tool_calls，对应 tool 消息在列表中

### E12: 角色交替合法

- [ ] **触发**：摘要 + 恢复 + 近期原文拼接后
- [ ] **预期**：`check_alternating()` 通过，不抛 ValueError
- [ ] recent 首条为 user → 中间插入了 assistant 衔接占位
