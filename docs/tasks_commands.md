# Tasks — 命令框架

> 对齐参考 spec 的 12 条命令 / Kind / UI Protocol / 补全,适配 CodeForge(prompt_toolkit)。核心包放 `core/commands/`,TUI 只做 UI 协议实现与分发桥接。

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `core/commands/__init__.py` | 包出口,re-export |
| 新建 | `core/commands/types.py` | Kind 枚举、Command / CommandCall / Handler 类型 |
| 新建 | `core/commands/registry.py` | Registry: register/lookup/visible/prefix_match + 冲突检测 |
| 新建 | `core/commands/parse.py` | parse_line 解析 |
| 新建 | `core/commands/ui.py` | UI Protocol + NopUI 测试桩 |
| 新建 | `core/commands/builtin_local.py` | /help /status /memory /permission /session |
| 新建 | `core/commands/builtin_ui.py` | /exit /plan /compact /resume /clear |
| 新建 | `core/commands/builtin_prompt.py` | /do /review + 提示词常量 |
| 新建 | `core/commands/builtins.py` | register_builtins(reg) 注册 12 条 |
| 新建 | `tests/test_commands.py` | 注册/解析/冲突/builtins 单测 |
| 删除 | `tui/commands.py` | 旧分发器与 handler |
| 改造 | `tui/app.py` | CodeForgeApp 实现 UI Protocol + cmd_registry + dispatch_slash + 补全接入 |
| 改造 | `tui/completer.py` | CommandCompleter(prompt_toolkit) |
| 改造 | `core/notes/store.py` | NoteStore 新增 list_files |
| 改造 | `core/tool/registry.py` | ToolRegistry 新增 count |
| 改造 | `core/agent/runtime.py` | SessionRuntime 新增 reset_for_new_session |

---

## T0a: NoteStore.list_files

**说明：** `core/notes/store.py` 新增 `list_files() -> tuple[list[str], list[str]]`,列出项目层 + 用户层 memory 目录下 `.md` 文件名(含 MEMORY.md),目录不存在视为空、OSError 告警后视为空,按字典序排序。

**影响文件：** `core/notes/store.py`;`tests/test_notes.py`
**依赖任务：** 无

## T0b: Writer.path

**说明：** `core/archive/writer.py` 的 `Writer` 已有 `path` property(返回 conversation.jsonl 绝对路径),直接复用;/session 展示用。

**影响文件：** 无改动(复用现有 `Writer.path`)
**依赖任务：** 无

## T0c: SessionRuntime.reset_for_new_session + ToolRegistry.count

**说明：**
- `core/agent/runtime.py` 新增 `reset_for_new_session(ses_ctx)` — 原子重置 replacement/recovery/auto_tracking/usage_anchor/anchor_msg_len/turn_count,`session` 指向新 ctx,`context_window` 保留
- `core/tool/registry.py` 新增 `count() -> int` — 返回已注册工具数

**影响文件：** `core/agent/runtime.py`、`core/tool/registry.py`
**依赖任务：** 无

---

## T1: 命令类型与 Kind 枚举

**说明：** `types.py`:
- `class Kind(Enum)`: `LOCAL="local"` / `UI="ui"` / `PROMPT="prompt"`
- `Handler = Callable[["UI"], Awaitable[None]]`(前向引用)
- `@dataclass(slots=True) class Command`: `name` / `description` / `kind` / `handler` / `aliases`(默认空) / `arg_hint`(默认 `""`,非空表示接受参数) / `hidden=False`
- `@dataclass class CommandCall`: `name` / `args`

**影响文件：** `core/commands/types.py`
**依赖任务：** 无

## T2: Registry + 冲突检测 + 前缀匹配

**说明：** `registry.py`:
- `_by_name: dict[str, Command]`(主名+别名都映射同一命令,key 已小写)、`_visible: list[Command]`
- `register(cmd)`:校验 name/aliases 小写非空;任一 key 已存在 → `raise RuntimeError(f"command conflict: {key}")`;非 hidden append 到 `_visible` 并按 name 字典序排序
- `lookup(name)` → 小写后查
- `visible()` → 拷贝
- `prefix_match(prefix)` → strip `/`、小写、前缀匹配 name(不匹配别名/描述),返回字典序

**影响文件：** `core/commands/registry.py`;`tests/test_commands.py`
**依赖任务：** T1

## T3: parse_line 解析

**说明：** `parse.py`:
- `parse_line(text) -> CommandCall | None | str`(哨兵区分「非命令」「未知/空」)
- 空/纯空白 → 空哨兵;不以 `/` 开头 → 非命令哨兵;`/` 开头 → 第一个空格前 name(小写),其后 args;无参 → args=""
- 支持 `Command.arg_hint`:带参数命令(/review)由 dispatch 决定是否传 args

**影响文件：** `core/commands/parse.py`;`tests/test_commands.py`
**依赖任务：** T1

## T4: UI Protocol + NopUI

**说明：** `ui.py`:
- `class UI(Protocol)`: `println(msg)` / `error(msg)` / `mode()` / `set_mode(m)` / `inject_and_send(label, preset)` / `usage_in()` / `usage_out()` / `model_name()` / `cwd()` / `tool_count()` / `memory_files()` / `session_path()` / `session_id()` / `quit()` / `force_compact()` / `open_resume_menu()` / `clear_and_new_session()` / `idle()`
- `class NopUI`: 全部写入 no-op、查询返回零值

**影响文件：** `core/commands/ui.py`
**依赖任务：** 无

## T5: 5 条纯本地命令

**说明：** `builtin_local.py`:
- `/help`: 闭包捕获 reg,`reg.visible()` 两列对齐输出
- `/status`: 6 行 `Mode:`/`Tokens:`/`Tools:`/`Memories:`/`Model:`/`Directory:`
- `/memory`: 打印 `memory_files()`;空时提示
- `/permission`: 打印 `mode().value`
- `/session`: 打印 `Session:` 与 `Path:`

**影响文件：** `core/commands/builtin_local.py`
**依赖任务：** T1, T2, T4

## T6: 5 条影响界面命令

**说明：** `builtin_ui.py`:
- `/exit`: `ui.quit()`
- `/plan`: `ui.set_mode(PLAN)` + println
- `/compact`: idle 守护 → `ui.force_compact()`
- `/resume`: idle 守护 → `ui.open_resume_menu()`
- `/clear`: `ui.clear_and_new_session()` + notice

**影响文件：** `core/commands/builtin_ui.py`
**依赖任务：** T1, T4

## T7: 2 条提示词命令

**说明：** `builtin_prompt.py`:
- 模块级 `REVIEW_DIRECTIVE = "请审查当前上下文中的代码变更/已读取的文件,指出潜在 bug、可读性问题和可简化处。"`;`EXECUTE_DIRECTIVE`(复用 `core/agent/plan_mode.build_plan_mode_exit_reminder`)
- `/do`: `set_mode(DEFAULT)` + `inject_and_send("/do", EXECUTE_DIRECTIVE)`
- `/review`: `inject_and_send("/review", REVIEW_DIRECTIVE + ("\n重点关注:" + args if args else ""))`

**影响文件：** `core/commands/builtin_prompt.py`
**依赖任务：** T1, T4

## T8: register_builtins

**说明：** `builtins.py`:
- `register_builtins(reg)`: 按一致顺序注册 12 条 `Command(...)`(/exit /plan /do /compact /resume /clear /help /status /memory /permission /session /review);/help 用工厂注入 reg 闭包;/review 带 `arg_hint="重点"`
- 复用旧命令名作别名:如 /compact←/compress、/memory←/notes、/permission←/mode、/session←/sessions、/resume←(无)

**影响文件：** `core/commands/builtins.py`;`tests/test_commands.py`
**依赖任务：** T5, T6, T7

## T9: TUI 实现 UI Protocol + dispatch_slash + 注册中心

**说明：** 改造 `tui/app.py`(删除 `tui/commands.py`):
- `CodeForgeApp` 增 `cmd_registry: Registry`、构造时 `register_builtins(reg)`;`__init__` 里实现 `UI` 全部方法(只读查询桥接 `self.runtime`/`self.writer`/`self.notes`/`self.registry`;写入方法 `println`/`error` 走 `console.print`,`set_mode` 改 `agent.set_permission_mode`,`quit` 触发退出,`force_compact` 复用 `run_force_compact`,`open_resume_menu`/`clear_and_new_session` 见 T10/T9c)
- `dispatch_slash(text) -> bool`: `parse_line` → 非命令返回 False;lookup miss → 提示「未知命令,输入 /help 查看可用命令」;`Kind in (UI, PROMPT)` 且非 idle → 「请等待当前任务完成」;否则 `await cmd.handler(self)`(异常捕获后 error 展示);返回 True
- `idle()` → `not agent_running`

**影响文件：** `tui/app.py`、删除 `tui/commands.py`;更新 `tests/test_agent_compression.py` 的命令注册断言
**依赖任务：** T8

## T9c: /clear 与 /do 的会话重建

**说明：** 在 `CodeForgeApp` 实现 `clear_and_new_session()`:
1. `self.writer.close()`
2. `new_ses_ctx = new_session_context(self.workspace)`
3. `new_writer = Writer(new_ses_ctx.session_dir, model=...)`
4. 重建 `ConversationManager(on_append=new_writer.append, on_replace=_make_on_replace(new_writer))`
5. `self.runtime.reset_for_new_session(new_ses_ctx)`
6. 归零 token 计数与回合数;console 输出 notice
旧 writer 关闭后 hook 失效,必须重建 conversation 才能挂上新 writer;旧 JSONL 保留,/resume 可见。

`inject_and_send(label, preset)`: `conversation.add_user_message(preset)` + 触发 Agent 回合(复用 `_inject_and_run`,后台 task)。

**影响文件：** `tui/app.py`
**依赖任务：** T0c, T9

## T10: open_resume_menu

**说明：** 把现有 `/resume` 恢复逻辑迁移为 `CodeForgeApp.open_resume_menu()`:构造会话列表、进入「恢复中」状态、显示行式选择器(现有 `_pick_session`),选中后走既有恢复流程;idle 守护已在 dispatch_slash 统一处理。

**影响文件：** `tui/app.py`、`tui/commands.py`(删除后逻辑并入)
**依赖任务：** T9

## T11: 补全菜单(prompt_toolkit)

**说明：** 新建 `tui/completer.py`:
- `CommandCompleter(Completer)`: `get_completions(document)` — 输入以 `/` 开头时,对 `registry.prefix_match(document.text)` 生成 `Completion(cmd.name, display_meta=cmd.description)`;hidden 不参与
- 接进 `PromptSession(completer=..., complete_while_typing=True)`:prompt_toolkit 原生渲染下拉菜单,↑/↓ 切换、Tab 循环、Esc 关闭
- 单匹配自动补全、多匹配弹菜单由 prompt_toolkit 内置完成
- 行为对齐 F24-F32:仅前缀匹配、删 `/` 关闭、含换行不激活(靠 prompt_toolkit 输入模型保证)

**影响文件：** `tui/completer.py`、`tui/app.py`;`tests/test_commands.py`(补全候选单测)
**依赖任务：** T2, T9

## T12: 接入主输入循环

**说明：** `tui/app.py` 主循环:
- 替换 `dispatch_command` 为 `if await app.dispatch_slash(text): continue`
- Enter 提交时若补全菜单激活且当前有选中命令 → 直接执行该命令(在 dispatch 前判断)
- 移除旧 `SlashCompleter` 与 `BUILTIN_COMMANDS` 引用

**影响文件：** `tui/app.py`
**依赖任务：** T9, T11

## T13: 迁移测试 + 端到端

**说明：**
- `tests/test_commands.py`: 冲突 panic / visible 排序 / prefix_match / parse 各形态 / NopUI 跑 12 handler 不抛 / RecordingUI 断言 /status 6 字段、/compact 非 idle 拒绝、/do set_mode+inject、/review 含 args、/help 含 12 名
- 更新 `tests/test_agent_compression.py` 旧命令断言
- `pytest tests/ -v` 全量通过;`ruff check core/commands/ tui/app.py` 无告警;`format --check` 通过
- 既有 /exit /plan /do /compact /resume 行为不变(N10)

**影响文件：** `tests/test_commands.py` 等
**依赖任务：** T1-T12 全部

---

## 执行顺序

```
T0a, T0c (并行) → T1 → (T2, T3, T4 并行) → (T5, T6, T7 并行) → T8 → T9 → T9c → T10 → T11 → T12 → T13
```
