# Tasks — 记忆与会话持久化

本章按 spec_memory 的 F1–F48 拆分为 15 个自包含任务。每个任务可在一次专注会话内完成，任务间通过「依赖」标明顺序。

## 文件清单

| 文件路径 | 类型 | 职责 |
|----------|------|------|
| `core/instructions/__init__.py` | 新建 | 重导出 discover_instructions / expand_instructions |
| `core/instructions/discovery.py` | 新建 | 三处 CODEFORGE.md 发现 + 优先级排序（F1） |
| `core/instructions/include.py` | 新建 | @include 展开（深度/防环/沙箱/二进制，F2–F6） |
| `core/instructions/inject.py` | 新建 | 指令文本注入 custom_instructions（F7/F43） |
| `core/context_compression/state.py` | 修改 | `_new_session_id()` 新格式 + `SessionContext.session_dir`（F9/F10） |
| `core/archive/__init__.py` | 新建 | 重导出 Writer / list_sessions / restore |
| `core/archive/writer.py` | 新建 | JSONL 追加 + fsync + 压缩标记 + 关闭（F11/F12/F14–F16） |
| `core/archive/session_list.py` | 新建 | 会话列表扫描与排序（F18–F20） |
| `core/archive/reader.py` | 新建 | 恢复流程（F21–F24） |
| `core/archive/cleanup.py` | 新建 | 过期会话清理（F25/F26） |
| `core/notes/__init__.py` | 新建 | 重导出 NoteStore / build_memory_index / update_notes |
| `core/notes/store.py` | 新建 | 笔记文件 + frontmatter + MEMORY.md 索引（F27–F31） |
| `core/notes/inject.py` | 新建 | 索引注入 long_term_memory + 25KB 截断（F32–F34） |
| `core/notes/updater.py` | 新建 | 记忆更新器（F35–F42/F47） |
| `core/prompts/modules.py` | 修改 | 填充 custom_instructions / long_term_memory 空槽 |
| `core/prompts/builder.py` | 修改 | 拼装器接受 instructions / memory 参数（F43） |
| `conversation/manager.py` | 修改 | on_append / on_replace 回调（F44） |
| `core/agent/runtime.py` | 修改 | SessionRuntime 追加 turn_count / writer 引用 |
| `core/agent/agent.py` | 修改 | turn_count 递增 + 记忆更新触发（F35） |
| `tui/commands.py` | 修改 | /resume + /notes + /sessions 命令 |
| `tui/app.py` | 修改 | 启动注入 / resume UI / 退出清理（F45/F46） |
| `tests/test_instructions.py` | 新建 | 指令/@include 单测 |
| `tests/test_archive.py` | 新建 | 存档/恢复/列表/清理单测 |
| `tests/test_notes.py` | 新建 | 笔记存储/更新/注入单测 |

---

## T1: 指令发现与加载

**说明：** 创建 `discovery.py`：
- 三个候选路径：`<project_root>/CODEFORGE.md`、`<project_root>/.codeforge/CODEFORGE.md`、`~/.codeforge/CODEFORGE.md`
- 按此顺序返回存在文件的有序列表（高优先级在前）；文件缺失静默跳过
- 每个文件标注其「根边界」：前两个为项目根，第三个为 `~/.codeforge/`
- 提供读取入口，返回按优先级拼接、级间空行分隔的合并文本

**影响文件：**
- `core/instructions/__init__.py` — 新建
- `core/instructions/discovery.py` — 新建

**依赖任务：** 无

**参考资料：**
- spec_memory F1
- `Path.cwd()` 项目根；`Path.home()` 用户根

---

## T2: @include 展开器

**说明：** 创建 `include.py`：
- 解析独占一行的 `@include <path>`（行首、`#` 注释外）；段落中间的 `@include` 不动
- 路径**相对当前文件所在目录**解析（F2）
- 深度上限 `MAX_INCLUDE_DEPTH = 5`，超深保留原文 + 警告注释（F3）
- 防环：visited 绝对路径集合，同链不重复加载 + 警告注释（F4）
- 沙箱：resolve 后 `Path.is_relative_to(根边界)` 校验，越界丢弃 + 警告注释（F5）
- 缺失静默；空文件产出空内容；前 512 字节含 `\x00` 判定二进制，跳过 + 警告注释（F6）
- 警告注释文本与 spec F3/F4/F5 逐字一致

**影响文件：**
- `core/instructions/include.py` — 新建

**依赖任务：** T1

**参考资料：**
- spec_memory F2–F6
- `Path.resolve()` / `Path.is_relative_to()`（Python 3.9+）

---

## T3: 指令注入 custom_instructions 模块

**说明：** 创建 `inject.py`，并让拼装器支持注入：
- `core/prompts/modules.py`：`custom_instructions`（priority 8）content 由注入器设置，`long_term_memory`（priority 10）留给 T12
- 指令注入：合并文本填入 `custom_instructions.content`；为空时保持空（拼装器自动跳过）
- `core/prompts/builder.py`：拼装器新增接受 instructions / memory 两个可选文本参数的方法，非空时填入对应模块（F43）
- 注入结果缓存到模块 content，进程生命周期内不变（F8）

**影响文件：**
- `core/instructions/inject.py` — 新建
- `core/prompts/modules.py` — 修改
- `core/prompts/builder.py` — 修改

**依赖任务：** T1, T2

**参考资料：**
- `core/prompts/modules.py:111-115` `_CUSTOM_INSTRUCTIONS`（content 当前为空）
- `core/prompts/builder.py:60-83` `build_assembly` 跳过空模块、全模块进缓存块
- spec_memory F7/F8/F43

---

## T4: Session ID 与目录改造

**说明：** 修改既有压缩子包：
- `core/context_compression/state.py` 的 `_new_session_id()`：格式改为 `YYYYMMDD-HHMMSS-xxxx`（本地时间 + 4 字符 hex），时间部分取进程启动时刻
- `SessionContext` 新增 `session_dir` 字段（`<workspace>/.codeforge/sessions/<session_id>`）；`spill_dir` 改为 `session_dir + "/tool-results"`
- `new_session_context()` 同时创建 session_dir 与 spill_dir
- 更新既有压缩测试中断言旧格式的用例（`tests/test_context_compression.py` 的 session_id 正则）

**影响文件：**
- `core/context_compression/state.py` — 修改
- `tests/test_context_compression.py` — 修改
- `core/context_compression/__init__.py` — 若重导出类型变化则同步

**依赖任务：** 无

**参考资料：**
- `core/context_compression/state.py:27-41` `_new_session_id()`；`:44-52` `SessionContext`；`:53-69` `new_session_context()`
- spec_memory F9/F10

---

## T5: JSONL 序列化 + 会话写入器 + 压缩标记

**说明：** 创建 `writer.py`：
- 序列化 `Message` → JSON 行：role/content/tool_use_id/tool_name/tool_input/status/id/ts；首行追加 `model`（来自 provider）
- `Writer`：持有文件句柄 + `asyncio.Lock`，`append(line)` 加锁 → 写入 → `flush()` + `os.fsync(fileno())`
- 压缩标记：`write_compact_marker()` 写 `{"type":"compact","ts":...}`（供 replace 时先写标记再写新消息）
- `close()` 关闭句柄；实现 `__enter__`/`__exit__`
- 只追加不重写；打开模式 `a`，崩溃最多丢最后一行不完整数据（F14）

**影响文件：**
- `core/archive/__init__.py` — 新建
- `core/archive/writer.py` — 新建

**依赖任务：** T4

**参考资料：**
- `conversation/message.py:30-42` Message 字段
- spec_memory F11/F12/F14–F16

---

## T6: ConversationManager 回调接入

**说明：** 修改 `conversation/manager.py`：
- 构造接受可选 `on_append: Callable[[Message], None]` 与 `on_replace: Callable[[list[Message]], None]`
- `add_user_message` / `add_assistant_message` / `add_tool_use` / `add_tool_result` 提交后调 `on_append`（F13）
- `replace_history()` 整体替换后调 `on_replace`，之前由上层先写 compact 标记（F44）
- 回调内部异常不得冒泡到对话主流程（try/except 包裹并告警）
- 未设置回调时行为与现有完全一致

**影响文件：**
- `conversation/manager.py` — 修改
- `tests/test_conversation.py` — 追加回调用例

**依赖任务：** T5

**参考资料：**
- `conversation/manager.py:42-164` add_* 系列与 replace_history
- spec_memory F13/F44

---

## T7: 会话列表扫描

**说明：** 创建 `session_list.py`：
- `list_sessions()` 扫描 `.codeforge/sessions/` 子目录，仅保留含 `conversation.jsonl` 者
- 每项信息：标题（首条 role=user 消息 content，截 50 字符）、相对时间（目录名时间戳 → "1 day ago" 等）、模型标签（首行 `model` 字段）、文件大小（`conversation.jsonl` stat）
- 无法解析为新格式 session ID 的目录跳过（旧格式不展示，N3）
- 按最后修改时间倒序

**影响文件：**
- `core/archive/session_list.py` — 新建

**依赖任务：** T4, T5

**参考资料：**
- spec_memory F18–F20
- `datetime.now() - timestamp` 计算相对时间

---

## T8: /resume 命令与选择 UI

**说明：** 修改 TUI：
- `tui/commands.py` 注册 `/resume`（`BUILTIN_COMMANDS` + 补全描述）
- `tui/app.py`：Agent 运行时输入 `/resume` → 提示「请等待当前任务完成」不进入列表（F46）
- 会话列表 UI：基于 prompt_toolkit 实现方向键导航 + 搜索过滤 + Enter 选择 + Esc 取消；选择后进入恢复流程
- 恢复中标记（agent_running 语义扩展）期间不允许发起新 `Agent.run`（F46）
- 恢复完成后显示 `已恢复会话 <session_id>，共 <N> 条消息`（F23）；原新会话 JSONL 保留不删（F24）

**影响文件：**
- `tui/commands.py` — 修改
- `tui/app.py` — 修改

**依赖任务：** T7, T9

**参考资料：**
- `tui/commands.py` BUILTIN_COMMANDS / dispatch_command 结构
- `tui/app.py` `_run_async` 输入循环与 `agent_running` 标志
- spec_memory F17/F19/F22–F24/F46

---

## T9: 恢复流程

**说明：** 创建 `reader.py`：
- `restore(jsonl_path, provider, context_window) → ConversationManager`：
  1. 从最后一个 `{"type":"compact"}` 标记之后逐行解析（F21.1）
  2. 坏行静默跳过并计数（F21.2）
  3. 末尾 assistant 消息带 `tool_name` 但无配对 tool 结果 → 截断到该条之前（F21.3）
  4. `estimate_tokens(0, msgs, 0)` 超 `window - summary_reserve - auto_safety_margin` → 先跑一次压缩（F21.4）
  5. 最后一条 ts 距当前 > 6 小时 → 追加时间提醒 user 消息（F21.5，文案含暂停时长）
- 恢复后切换会话：重建 `ConversationManager`、重新打开该会话 `Writer`（追加模式）、替换 `SessionContext`（F22）
- 任一步失败有降级：坏行跳过、截断失败保留、压缩失败用原文

**影响文件：**
- `core/archive/reader.py` — 新建

**依赖任务：** T5, T6

**参考资料：**
- `core/context_compression/token.py:71-91` estimate_tokens
- `core/context_compression/compact.py` manage_context / TriggerKind
- `core/context_compression/const.py` SUMMARY_RESERVE / AUTO_SAFETY_MARGIN
- spec_memory F21/F22

---

## T10: 会话清理

**说明：** 创建 `cleanup.py`：
- `cleanup_expired(workspace, days=30)`：扫描 `.codeforge/sessions/`，解析目录名 session ID 的时间戳部分，距当前 > 30 天 → 删除整个子目录（含 JSONL 与 tool-results）
- 无法解析为 `YYYYMMDD-HHMMSS-xxxx` 的目录跳过（N3，不误删旧格式）
- 单个目录删除失败（OSError）跳过不影响其他
- 由启动流程在后台 asyncio task 执行，不阻塞启动（F26）

**影响文件：**
- `core/archive/cleanup.py` — 新建

**依赖任务：** T4

**参考资料：**
- spec_memory F25/F26
- `shutil.rmtree` 删除目录

---

## T11: 笔记存储层

**说明：** 创建 `store.py`：
- 目录：项目级 `.codeforge/memory/`、用户级 `~/.codeforge/memory/`（不存在时创建）
- 单条笔记 = 独立 .md 文件，带 YAML frontmatter（type/title/created/updated，F28）
- 文件名 `<type>_<slug>.md`（F31）
- 每级 `MEMORY.md` 索引：`- [<type>] <title> — <一句话描述>`，每行一条（F30）
- 操作：`create_note`（写文件 + 索引追加行）、`update_note`（重写文件与 frontmatter + 索引更新行）、`delete_note`（删文件 + 索引删行）、`list_index(level)` 读索引
- memory 目录写操作用一把锁保护（N2）

**影响文件：**
- `core/notes/__init__.py` — 新建
- `core/notes/store.py` — 新建

**依赖任务：** 无

**参考资料：**
- spec_memory F27–F31
- `yaml.safe_dump` 写 frontmatter；frontmatter 与正文以 `---` 分隔

---

## T12: 记忆索引注入

**说明：** 创建 `inject.py`：
- `build_memory_index()`：拼接项目级 + 用户级 `MEMORY.md` 内容（项目级在前、用户级在后）
- 拼接结果超 25KB → 截断并追加 `(index truncated)`（F34）
- 注入 `core/prompts/modules.py` 的 `long_term_memory`（priority 10）content（F32）
- 注入文本为索引纯文本，非笔记全文；含提示：需要详情用文件读取工具读对应笔记文件（F33）
- 启动时与每次笔记更新后刷新

**影响文件：**
- `core/notes/inject.py` — 新建
- `core/prompts/modules.py` — 修改
- `core/prompts/builder.py` — 修改（与 T3 同处）

**依赖任务：** T3, T11

**参考资料：**
- `core/prompts/modules.py:123-127` `_LONG_TERM_MEMORY`
- spec_memory F32–F34/F43

---

## T13: 记忆更新器

**说明：** 创建 `updater.py`：
- `async update_memory(provider_config, notes_index, recent_messages, memory_dir_map) -> None`：
  - 构造记忆更新请求：两级现有索引 + 最近一轮对话（最后 user → 最终回复），不传工具（F37/F38）
  - LLM 返回结构化 JSON 数组（F39）：create（level/type/title/slug/content）/ update（filename/title/content）/ delete（filename）；`[]` 表示无更新
  - 解析并执行操作（F40）：调 `store.create_note` / `update_note` / `delete_note`
  - 解析失败或 JSON 无效 → 静默记录日志，不做重试（F42）
  - 任何异常（含 PTL）不抛给上层
- 触发判断：`should_trigger_memory(turn_count, user_input)` — `turn_count % 5 == 0` 或用户消息含「记住/记忆/别忘/remember/memo」（F35）
- `asyncio.create_task` 后台执行，不阻塞输入（F36）；只读对话快照、只写 memory 目录（F47）

**影响文件：**
- `core/notes/updater.py` — 新建

**依赖任务：** T11

**参考资料：**
- `llm/client.py` LLMClient.create / stream_chat；tools=None 模式见 `core/context_compression/layer2.py` summarize_once
- `core/context_compression/summary_prompt.py:57-99` serialize_conversation（复用对话序列化）
- spec_memory F35–F42/F47

---

## T14: 笔记与会话操作命令

**说明：** 修改 `tui/commands.py`：
- `/notes` — 打印四类索引；`/notes <类别>` — 打印该类笔记全文；`/notes edit` — 打印文件路径；`/notes clear [类别]` — 二次确认后清空（F48）
- `/sessions` — 复用 T7 列表打印；`/sessions clear` — 二次确认后清空存档
- 清空类命令在确认前不执行任何写操作

**影响文件：**
- `tui/commands.py` — 修改
- `tui/app.py` — 修改（app 容器持有 notes 引用）

**依赖任务：** T7, T11

**参考资料：**
- `tui/commands.py` BUILTIN_COMMANDS / dispatch_command
- spec_memory F48/AC30

---

## T15: 接入主流程 + 端到端验证

**说明：** 接通启动、循环、退出三处，并验证：
- 启动（`tui/app.py`）：① 加载指令（T1–T3）→ ② 初始化记忆并加载索引（T11/T12）→ ③ 后台清理（T10）→ ④ 指令文本与记忆文本传入拼装器（F45）
- 循环（`core/agent/agent.py` + `core/agent/runtime.py`）：SessionRuntime 追加 `turn_count` 与 writer 引用；`run()` 完成（最终回复无工具调用）后 `turn_count += 1`，满足条件时 `asyncio.create_task(update_memory(...))`（F35）
- 存档：`ConversationManager` 注入 on_append/on_replace 回调 → writer 自动追加（T6）；压缩替换时先写 compact 标记（F12）
- 退出（`tui/app.py` finally）：writer.close()；等待后台记忆任务（带超时）
- 验证：`pytest tests/ -v` 全量通过（含新增三个测试文件）；`ruff check` 相关包无新增告警；`ruff format --check` 通过

**影响文件：**
- `core/agent/runtime.py` — 修改
- `core/agent/agent.py` — 修改
- `tui/app.py` — 修改
- `tests/test_instructions.py` / `tests/test_archive.py` / `tests/test_notes.py` — 新建

**依赖任务：** T3, T6, T8, T9, T10, T12, T13, T14

**参考资料：**
- `core/agent/agent.py:154-315` run() 主循环（AgentFinished 分支）
- `core/agent/runtime.py` SessionRuntime dataclass
- `tui/app.py` `_run_async` 启动/退出段
- `tests/test_agent_compression.py` monkeypatch LLMClient.create 模式
- checklist_memory.md 验收项
