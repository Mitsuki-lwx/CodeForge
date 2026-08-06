# Checklist — 记忆与会话持久化

每一项通过运行代码或观察行为验证。条目格式：操作 → 预期可观测结果。

---

## 实现完整性

### 项目指令

- [ ] `ls core/instructions/` 列出 discovery.py / include.py / inject.py / __init__.py
- [ ] `grep -rn "CODEFORGE.md" core/instructions/` 命中 ≥3 处
- [ ] 三路径各放一份 CODEFORGE.md → 注入结果顺序为：根 > `.codeforge/` > `~/.codeforge/`
- [ ] 仅根目录有 CODEFORGE.md → 加载成功，只含根内容，无报错
- [ ] `grep -n "MAX_INCLUDE_DEPTH = 5" core/instructions/include.py` 命中
- [ ] `@include rules/style.md`（相对当前文件目录）→ 引用内容替换该行
- [ ] 6 层嵌套 @include → 第 6 层保留原文，出现 `<!-- @include 超过最大嵌套深度，已跳过:` 注释
- [ ] A include B、B include A → 第二次不展开，出现 `<!-- @include 检测到环路，已跳过:` 注释
- [ ] 项目级 CODEFORGE.md 写 `@include ../../etc/passwd` → 不加载，出现 `<!-- @include 路径超出允许范围，已跳过:` 注释
- [ ] `@include missing.md` → 静默跳过，不报错
- [ ] 二进制文件（前 512 字节含 `\x00`）→ 跳过并追加警告注释
- [ ] `@include` 出现在段落中间 → 保持原文，不展开
- [ ] `grep -n "custom_instructions" core/prompts/modules.py` 命中，注入后 content 非空
- [ ] 注入内容为空时 custom_instructions 保持空，拼装跳过（行为同既有）

### 会话存档

- [ ] 启动进程 → session ID 形如 `20260601-143022-a1b2`（`YYYYMMDD-HHMMSS-xxxx`）
- [ ] `core/context_compression/state.py` 中 `SessionContext` 含 `session_dir` 字段，`spill_dir` 为 `session_dir/tool-results`
- [ ] 发送一条消息、得到回复 → `conversation.jsonl` ≥ 2 行（user + assistant），每行合法 JSON，含 role/content/ts
- [ ] 首行额外含 `model` 字段
- [ ] 触发一次压缩 → JSONL 出现 `{"type":"compact","ts":...}` 标记行，其后跟压缩后消息
- [ ] `grep -n "asyncio.Lock\|fsync" core/archive/writer.py` 命中
- [ ] `Writer` 实现 `__enter__` / `__exit__`，退出时 `close()` 关闭句柄
- [ ] 崩溃模拟：向 JSONL 末尾追加一行非法 JSON → 之前所有行正常解析

### 会话恢复

- [ ] `/resume` 在 `tui/commands.py` 的 BUILTIN_COMMANDS 中注册，补全描述含「恢复」
- [ ] 存在 3 个有效会话 → `/resume` 列表展示 3 项，每项有标题/相对时间/模型标签/文件大小
- [ ] 列表中输入搜索关键词 → 只展示标题匹配项
- [ ] 列表按最后修改时间倒序（最新在前）
- [ ] 标题截断到 50 字符（超长含省略号）
- [ ] JSONL 插入一行无效 JSON → 恢复时该行跳过，其余消息完整
- [ ] JSONL 末尾为带 `tool_name` 的 assistant 消息、无配对 tool 结果 → 恢复时截断，以上一条完整消息结尾
- [ ] 构造超阈值 JSONL → 恢复过程触发一次压缩（可观测：压缩日志 / 消息数骤减）
- [ ] 最后消息 ts 距今 > 6 小时 → 对话末尾追加 `[系统提示] 本会话已暂停 ...` 提醒
- [ ] 恢复后发新消息 → 追加到同一个 JSONL，行号递增
- [ ] `Agent.run` 期间输入 `/resume` → 提示「请等待当前任务完成」，不进入列表
- [ ] 恢复完成后显示 `已恢复会话 <session_id>，共 <N> 条消息`
- [ ] 原新会话的 JSONL 保留在磁盘，不被删除

### 会话清理

- [ ] 手动创建 31 天前时间戳的 session 目录 → 启动后被删除（含子目录内容）
- [ ] 手动创建旧格式 session ID 目录（如 `1717000000-abc12345`）→ 启动后不删除、不在 /resume 列表
- [ ] 清理在后台 task 执行，启动不被阻塞

### 自动笔记

- [ ] 对话中说「回复简洁点」→ Agent 回复后 memory 目录出现 `user_preference_*.md`，frontmatter 含 type/title/created
- [ ] 创建笔记后对应级别 `MEMORY.md` 出现 `- [<type>] <title> — ...` 行
- [ ] 笔记文件名格式 `<type>_<slug>.md`（全小写、下划线）
- [ ] 项目级笔记落在 `.codeforge/memory/`，用户级笔记落在 `~/.codeforge/memory/`
- [ ] `grep -n "long_term_memory" core/prompts/modules.py` 命中，索引注入后 content 非空
- [ ] 注入内容是索引纯文本，非笔记全文；含「用文件读取工具读取完整笔记」提示
- [ ] 构造 > 25KB 的 MEMORY.md → 注入时截断到 25KB 并含 `(index truncated)`
- [ ] 记忆更新请求 `tools=None`（记录请求体断言）
- [ ] LLM 返回 `[]` → memory 目录无任何变更
- [ ] LLM 返回 create/update/delete → 对应文件与索引行按操作变更
- [ ] mock provider 对记忆更新返回错误 → 主会话不受影响，仅日志记录
- [ ] 记忆更新执行中用户发送下一条消息 → 消息立即处理，不等更新完成
- [ ] `SessionRuntime.turn_count % 5 == 0` 触发；用户消息含「记住/记忆/别忘/remember/memo」也触发

### 操作命令（F48）

- [ ] `/notes` → 打印四类索引，不发 LLM
- [ ] `/notes preferences` → 打印该类笔记全文
- [ ] `/notes clear` → 先确认后删除，确认前文件未变
- [ ] `/notes edit` → 打印笔记文件路径
- [ ] `/sessions` → 列出会话
- [ ] `/sessions clear` → 先确认后清空

### 编译与测试

- [ ] `python -c "import core.instructions, core.archive, core.notes"` 退出码 0
- [ ] `grep -rn "参考\|取自\|对齐.*实现\|mirror\|镜像" core/instructions/ core/archive/ core/notes/` 无命中
- [ ] `ruff check core/instructions/ core/archive/ core/notes/` 无告警
- [ ] `ruff format --check core/instructions/ core/archive/ core/notes/` 输出为空
- [ ] `pytest tests/test_instructions.py tests/test_archive.py tests/test_notes.py -v` 全部通过
- [ ] `pytest tests/ -v` 全量通过（不破坏既有 200+ 测试，含 compression 的 session ID 用例已更新）

---

## 端到端场景

### E1: 启动注入指令 + 记忆索引

- [ ] **触发**：根目录 CODEFORGE.md 含 `@include docs/coding-rules.md`；memory 目录有笔记
- [ ] **预期**：启动后首轮请求的 system 提示含指令展开文本 + 记忆索引；custom_instructions 与 long_term_memory 模块均非空；`grep -c "来自"` ≥ 1

### E2: 一轮对话后存档 + 自动笔记

- [ ] **触发**：TUI 完成一轮对话（Agent 给出最终回复）
- [ ] **预期**：`conversation.jsonl` 行数 ≥ 本轮消息数；首行含 model；退出前 memory 目录某类笔记被创建或更新

### E3: 恢复容错

- [ ] **触发**：向 JSONL 末尾追加非法 JSON，并在倒数第二行造无配对 tool 调用
- [ ] **预期**：`/resume` 恢复成功；悬空 tool 调用被截断；非法行被跳过；恢复后发消息正常追加到同一文件

### E4: 恢复超限触发压缩

- [ ] **触发**：存档一个超大对话（估算 > `window - 33000`），恢复该会话
- [ ] **预期**：恢复过程触发一次压缩，`conv.messages()` 条数明显减少；Agent 可继续对话

### E5: 时间跨度提醒

- [ ] **触发**：修改存档最后一条消息 ts 为 2 天前，恢复该会话
- [ ] **预期**：恢复后对话末尾出现 `[系统提示] 本会话已暂停 ...` 提醒，含时长
