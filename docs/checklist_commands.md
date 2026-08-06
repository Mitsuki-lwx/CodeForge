# Checklist — 命令框架

每一项通过运行代码或观察行为验证。条目格式：操作 → 预期可观测结果。

---

## 实现完整性

- [ ] `ls core/commands/` 列出 types.py / registry.py / parse.py / ui.py / builtins.py / builtin_local.py / builtin_ui.py / builtin_prompt.py / __init__.py
- [ ] `python -c "from core.commands import CommandRegistry, parse_line, Kind, UI"` 退出码 0
- [ ] `register_builtins(reg)` 恰好注册 12 条(/exit /plan /do /compact /resume /clear /help /status /memory /permission /session /review)
- [ ] 12 条命令名全小写、互不重复

## 注册中心

- [ ] 注册同名命令 → `raise RuntimeError`，异常信息含冲突名
- [ ] 注册的命令名撞已有别名、别名撞已有命令名 → 均 `raise RuntimeError`，含冲突键
- [ ] `visible()` 返回按 name 字典序排序的可见命令
- [ ] `prefix_match("/s")` 仅命中以 s 开头的命令名（/session、/status），不含别名/描述匹配
- [ ] `hidden=True` 的命令不在 `visible()`，但 `lookup` 仍命中
- [ ] 冲突时 `tui/app.py` 启动打印错误并以非 0 退出码终止

## 解析器

- [ ] `parse_line("")` / `parse_line("   ")` → 早返回空，不参与分发
- [ ] `parse_line("hello world")` → 非命令信号
- [ ] `parse_line("/plan")` → name=`plan`，args=`""`
- [ ] `parse_line("/PLAN")` → name 归一化为 `plan`
- [ ] `parse_line("/review 注意安全")` → name=`review`，args=`注意安全`
- [ ] 无 arg_hint 的命令携带参数（如 `/help xx`）→ 按未命中处理，提示 /help
- [ ] 输入 `/foobar` → 提示含「未知命令」与「/help」，不发 LLM

## 命令执行与输出

- [ ] `/help` → 输出 12 条「命令名 + 描述」两列对齐，字典序
- [ ] `/status` → 按顺序输出 6 行 key：`Mode:` / `Tokens:` / `Tools:` / `Memories:` / `Model:` / `Directory:`
- [ ] `/memory` → 至少列出 `MEMORY.md`（存在时），只列文件名
- [ ] `/permission` → 输出当前权限模式 value（default/plan/acceptEdits/bypassPermissions 之一）
- [ ] `/session` → 输出 `Session:` 与 `Path:` 两行
- [ ] `/clear` → 对话区清空、token 计数归零；旧会话 JSONL 保留（`ls .codeforge/sessions/` 数量 +1）
- [ ] `/plan` → 模式切到 PLAN，状态栏出现 `[PLAN]`
- [ ] `/do` → 模式切回 DEFAULT，Agent 收到含「按计划执行」的提示词
- [ ] 非计划模式 `/do` → 仍切 DEFAULT，不注入计划内容
- [ ] `/compact` → 走压缩事件流，显示 `已压缩，token 从 X 降至 Y`
- [ ] `/resume` → 打开会话列表，可选中恢复
- [ ] `/exit` → 进程退出，后台任务收到取消
- [ ] 非 idle（Agent 运行中）时 `/compact` / `/resume` → 提示「请等待当前任务完成」
- [ ] LOCAL 命令（/help /status /memory /permission /session）任何状态可执行
- [ ] `/review` → 状态栏进入流式，AI 开始回复；会话存档新增 user 消息含「审查」
- [ ] `/review 注意安全` → 提示词额外含「注意安全」

## UI 抽象

- [ ] `grep -rn "prompt_toolkit\|rich" core/commands/` 无命中（框架层不绑定渲染）
- [ ] handler 签名统一 `(ui, args)` 且只依赖 `UI` 协议
- [ ] `NopUI` 可用：`python -c "from core.commands.ui import NopUI; NopUI().mode()"` 不报错
- [ ] 12 个 handler 在 `NopUI` 上 `await` 不抛异常

## 补全

- [ ] 输入首字符 `/` → 菜单激活，显示 12 条候选
- [ ] 输入 `/s` → 菜单只剩 /session、/status
- [ ] 删空 `/` → 菜单立即关闭
- [ ] ↑/↓ 切换高亮；Enter 执行当前高亮命令
- [ ] ESC 关闭菜单，输入框内容保留
- [ ] 候选显示「命令名 + 描述」两列
- [ ] 隐藏命令不出现在补全候选

## 迁移

- [ ] `tui/commands.py` 已删除
- [ ] `grep -rn "dispatch_command\|BUILTIN_COMMANDS\|SlashCompleter" tui/` 无残留
- [ ] 旧命令名 `/compress` `/notes` `/mode` `/sessions` 作为别名仍可用

## 编译与测试

- [ ] `pytest tests/test_commands.py -v` 全部通过
- [ ] `pytest tests/ -v` 全量通过（不破坏既有 280+ 测试）
- [ ] `ruff check core/commands/ tui/app.py` 无告警；`ruff format --check` 通过
- [ ] `python -c "import core.commands, tui.app"` 退出码 0

---

## 端到端场景（tmux 实跑）

### A 启动与 /help

- [ ] 启动后键入 `/` → 补全菜单弹出含 12 条；键入 `/s` → 过滤为 /session、/status
- [ ] `/help` → 12 条字典序命令名/描述

### B 纯本地命令

- [ ] `/status` → 6 行 key:value，Mode 值与状态栏一致；/help /status /permission /memory /session 跑完后 token 计数仍为 0（纯本地不耗 token）

### C 补全键位

- [ ] `/s` 后按 ↓ 切高亮、Enter 执行所选（如 /status 出现 `Mode:`）
- [ ] `/s` 后按 ESC → 菜单消失、输入框保留 `/s`；删空 → 菜单消失

### D 影响界面命令

- [ ] `/plan` → 状态栏 `[PLAN]`；`/do` → 回 `[DEFAULT]` 且 AI 开始回复
- [ ] `/compact` → 压缩进度 notice + token 下降
- [ ] `/clear` → 新会话 notice；`ls .codeforge/sessions/` 数量 +1

### E 提示词命令

- [ ] `/review` → 状态栏流式 + AI 回复；`tail` 最新 JSONL 含 role=user 且文本含「审查」

### F 未命中与异常

- [ ] `/foobar` → 含「未知命令」与「/help」；token 计数不变；JSONL 无新增 assistant 行
- [ ] 空回车/纯空白回车 → 无任何输出新增

### G 启动期冲突检测

- [ ] 临时把 /help 注册两次 → 启动立即抛 RuntimeError 退出，异常含「help」；还原
