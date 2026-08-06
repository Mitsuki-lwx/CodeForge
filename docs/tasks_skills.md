# Tasks — Skill 系统

> 顺序执行。每完成一个任务跑 `ruff check core/skills/ core/tool/tools/load_skill.py core/tool/tools/install_skill.py` 确认无告警。接入主流程的任务（T7、T8、T9）做完后立刻补一次端到端验证再进下一项。

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `core/skills/__init__.py` | 包出口，re-export |
| 新建 | `core/skills/types.py` | SkillMeta / SkillDef / SkillSource / ActiveEntry |
| 新建 | `core/skills/parser.py` | 解析 SKILL.md frontmatter + body |
| 新建 | `core/skills/loader.py` | SkillLoader 两级路径 + 热重载 |
| 新建 | `core/skills/active.py` | ActiveSkills 跨轮激活列表 |
| 新建 | `core/skills/render.py` | render_body（$ARGUMENTS 替换 + 工具提示） |
| 新建 | `core/skills/executor.py` | SkillExecutor（inline / fork）+ filter_tool_registry |
| 新建 | `core/skills/install.py` | install_from_url（GitHub API 下载 + zip 防护） |
| 新建 | `core/skills/adapter.py` | to_prompt_items / to_prompt_entries 桥接 prompt 包 |
| 新建 | `core/tool/tools/load_skill.py` | LoadSkill 工具（系统工具，不受白名单约束） |
| 新建 | `core/tool/tools/install_skill.py` | InstallSkill 工具（远程安装） |
| 新建 | `core/commands/builtin_skill.py` | `/skill list \| info <name> \| reload` |
| 新建 | `core/commands/skill_register.py` | register_skills_as_commands / remove_skill_commands |
| 修改 | `core/tool/interface.py` | Tool ABC 新增 `is_system_tool` 属性 |
| 修改 | `core/tool/registry.py` | 新增 `definitions_filtered` + `system_definitions` |
| 修改 | `core/agent/runtime.py` | SessionRuntime 新增 `active_skills` 字段；reset_for_new_session 清空 |
| 修改 | `core/agent/agent.py` | 新增 activate_skill / clear_active_skills / set_skill_catalog；每轮 env 拼接 |
| 修改 | `core/prompts/modules.py` | `_ACTIVE_SKILLS` → skills-catalog（内容来自 catalog） |
| 修改 | `core/prompts/environment.py` | `collect_environment` 扩展接受 active_skills 参数 |
| 修改 | `core/commands/builtins.py` | /review 替换为 review Skill 命令；/clear 追加清理 active_skills |
| 修改 | `core/commands/ui.py` | UI 协议新增 4 个方法 + NopUI 兜底 |
| 修改 | `core/commands/__init__.py` | 新增 export |
| 修改 | `tui/app.py` | CodeForgeApp 实现新 UI 方法 + 启动期接线 |
| 新建 | `.codeforge/skills/commit/SKILL.md` | 内置 commit Skill |
| 新建 | `.codeforge/skills/review/SKILL.md` | 内置 review Skill |
| 新建 | `.codeforge/skills/test/SKILL.md` | 内置 test Skill |
| 新建 | `tests/test_skills.py` | 单测 |

---

## T1: Skill 数据结构与 frontmatter 解析

**说明**：`core/skills/types.py` + `core/skills/parser.py`（新建）

`types.py`:
- `SkillSource` 枚举：`USER = "user"` / `PROJECT = "project"`
- `@dataclass(slots=True) class SkillMeta`：字段 `name / description / allowed_tools / mode / fork_context / model`；`allowed_tools` 默认空列表；`mode` 默认 `"inline"`；`fork_context` 默认 `"none"`
- `@dataclass(slots=True) class SkillDef`：`meta: SkillMeta` / `prompt_body: str` / `source_path: Path` / `source: SkillSource` / `is_directory: bool`
- `@dataclass(slots=True) class ActiveEntry`：`name: str` / `body: str`

`parser.py`:
- `SkillParseError(Exception)` 自定义异常
- `parse_skill_file(path: Path, source: SkillSource) -> SkillDef`：读文件 → 分离 frontmatter（两行 `---` 之间）→ `yaml.safe_load` → 构造 `SkillMeta` → 组装 `SkillDef`
- `_validate_meta(meta: dict) -> SkillMeta`：校验 `name` 正则 `^[a-z][a-z0-9\-]*$`、`mode in {inline, fork}`（其他值 warning 后按 inline）、`fork_context in {none, recent, full}`（缺省 none）、`allowed_tools` 为 list 或空
- `_split_frontmatter(raw: str) -> tuple[dict, str]`：解析 `---\n...\n---\n` 格式，缺开头 `---` 抛 `SkillParseError`

**影响文件**：`core/skills/types.py`、`core/skills/parser.py`
**依赖任务**：无

## T2: SkillLoader 两级搜索与热重载

**说明**：`core/skills/loader.py`（新建）

- 常量：`PROJECT_SKILLS_DIR = ".codeforge/skills"` / `USER_SKILLS_DIR = "~/.codeforge/skills"`
- `class SkillLoader`：
  - `__init__(work_dir: Path)`：计算 `_project_dir` / `_user_dir` 绝对路径
  - `load_all()`：先扫 project 目录再扫 user 目录；`_scan_directory(path, source)` 遍历子目录找 `SKILL.md` → `parse_skill_file`；维护 `_skills: dict[str, SkillDef]`（首次出现的 name 占位，后续同名跳过）和 `_cache: dict[str, SkillDef]`（热重载失败回退）
  - `get(name: str) -> SkillDef | None`：命中后 `parse_skill_file(source_path)` 强制重读；成功更新 `_cache`；失败回退 `_cache` 中旧版本并 `logger.warning`
  - `list_all() -> list[SkillDef]`：按名字典序返回全部
  - `names() -> list[str]`：按字典序返回全部名字
  - `get_source_label(name: str) -> str`：按路径前缀返回 `"project"` 或 `"user"`
  - `_scan_directory(path, source)`：遍历子目录，有 `SKILL.md` 即视为 Skill；区分单文件型和目录型（有 references 子目录的标记 `is_directory=True`）
  - 单个文件解析失败用 `logger.warning("Skipping skill '%s': %s", dir_name, e)` 并继续

**影响文件**：`core/skills/loader.py`
**依赖任务**：T1

## T3: ActiveSkills 与 render_body

**说明**：`core/skills/active.py` + `core/skills/render.py`（新建）

`active.py`:
- `class ActiveSkills`：
  - `_entries: list[ActiveEntry]`（保持激活顺序）、`_index: dict[str, int]`（name → 下标）
  - `activate(name, body)`：已在列表中则原地更新 body；否则追加
  - `clear()`：清空 `_entries` 和 `_index`
  - `snapshot() -> list[ActiveEntry]`：拷贝当前列表
  - `names() -> list[str]`：返回激活中的 Skill 名字列表

`render.py`:
- `render_body(skill: SkillDef, args: str) -> str`：
  1. `body = skill.prompt_body.replace("$ARGUMENTS", args)`
  2. 如果 body 不含 `$ARGUMENTS` 且 args 非空，在末尾追加 `\n\n## User Request\n\n{args}`
  3. 如果 `skill.meta.allowed_tools` 非空，在 body 顶部插 `**This skill is designed to use only these tools: {tools}. Prefer them over other tools when possible.**\n\n---\n\n`
  4. 返回渲染后的 body

**影响文件**：`core/skills/active.py`、`core/skills/render.py`
**依赖任务**：T1

## T4: 工具白名单过滤 + 系统工具豁免

**说明**：修改 `core/tool/interface.py` 和 `core/tool/registry.py`

`core/tool/interface.py`:
- Tool ABC 新增属性 `is_system_tool: bool = False`（类级默认值）

`core/tool/registry.py`:
- 新增 `definitions_filtered(self, allowed: list[str]) -> ToolRegistry`：
  - 返回新 `ToolRegistry` 实例
  - `allowed` 为空时拷贝全部工具
  - 遍历原 registry：`tool.is_system_tool` 为 True 的工具自动透传；其余只保留 name 在 `allowed` 中的
  - `allowed` 中出现不存在的工具名 → `raise SkillDependencyError(f"tool '{name}' not found for skill '{skill_name}'")`
- 新增 `system_definitions(self) -> list[Tool]`：返回 `is_system_tool=True` 的工具列表

`core/skills/executor.py` 同步定义：
- `SkillDependencyError(Exception)` 异常类
- `SYSTEM_TOOL_NAMES = frozenset({"LoadSkill"})` 常量

**影响文件**：`core/tool/interface.py`、`core/tool/registry.py`、`core/skills/executor.py`
**依赖任务**：无（与 T1-T3 并行）

## T5: SkillExecutor（inline + fork）

**说明**：`core/skills/executor.py`（继续）

`class SkillExecutor`:
- `__init__(self, catalog: SkillLoader, runtime: SessionRuntime, registry: ToolRegistry, provider, workspace: Path)` — 持有引用
- `async execute_inline(self, skill_name: str, args: str, ui: UI, agent: Agent) -> None`：
  1. `skill = self._catalog.get(skill_name)` → None 则 `ui.error`
  2. `body = render_body(skill, args)`
  3. `agent.activate_skill(skill_name, body)`
  4. `await ui.inject_and_send(f"/{skill_name}", body)`
- `async execute_fork(self, skill_name: str, args: str) -> str`：
  1. `skill = self._catalog.get(skill_name)` → None 返回错误字符串
  2. `body = render_body(skill, args)`
  3. 构造 fork `ConversationManager`，按 `fork_context` 装填历史：
     - `none`：仅 `add_user_message(body)`
     - `recent`：从主 conversation 取最近 5 条 user/assistant 消息 + `add_user_message(body)`
     - `full`：对主 conversation 做摘要 → `add_user_message(f"## Previous conversation summary\n\n{summary}\n\n---\n\n{body}")`
  4. `fork_registry = self._registry.definitions_filtered(skill.meta.allowed_tools)`（失败返回错误字符串）
  5. 选 provider：`skill.meta.model` 非空时构造新 LLMClient
  6. 临时 `Agent(registry=fork_registry, llm_client=..., exec_ctx=..., conversation=fork_conv, ...)`
  7. `async for event in fork_agent.run(body)` → 收集 `TextDelta` 文本到 `LoopComplete`
  8. 累计 token 写回主 `runtime.usage_anchor`
  9. 返回收集的 final_text

`filter_tool_registry(registry: ToolRegistry, allowed: list[str], skill_name: str) -> ToolRegistry`（独立函数）：
- `allowed` 为空 → 返回原 registry
- 调用 `registry.definitions_filtered(allowed)`，缺工具抛 `SkillDependencyError`

**影响文件**：`core/skills/executor.py`
**依赖任务**：T2, T3, T4

## T6: Agent 集成（active_skills + skill_catalog + env 拼接）

**说明**：修改 `core/agent/runtime.py` + `core/agent/agent.py` + `core/prompts/environment.py`

`core/agent/runtime.py`:
- `SessionRuntime` 新增字段 `active_skills: ActiveSkills = field(default_factory=ActiveSkills)`
- `reset_for_new_session` 追加 `self.active_skills.clear()`

`core/agent/agent.py`:
- `__init__` 新增可选参数 `skill_catalog: str = ""`
- 新增方法：
  - `activate_skill(name: str, body: str)` → `self._runtime.active_skills.activate(name, body)`
  - `clear_active_skills()` → `self._runtime.active_skills.clear()`
  - `set_skill_catalog(catalog: str)` → `self._skill_catalog = catalog`
- `run()` 每轮构造 env 时：
  ```python
  env_text = collect_environment(..., active_skills=self._runtime.active_skills.snapshot())
  if self._skill_catalog:
      env_text = self._skill_catalog + "\n\n" + env_text
  ```

`core/prompts/environment.py`:
- `collect_environment` 新增参数 `active_skills: list[ActiveEntry] | None = None`
- 当 `active_skills` 非空时在环境信息末尾追加 `## Active Skills` 段，每个 skill 渲染为 `### Skill: {name}\n\n{body}\n`

**影响文件**：`core/agent/runtime.py`、`core/agent/agent.py`、`core/prompts/environment.py`
**依赖任务**：T3（需要 ActiveEntry 类型）

## T7: LoadSkill 工具 + InstallSkill 工具

**说明**：`core/tool/tools/load_skill.py` + `core/tool/tools/install_skill.py`（新建）

`core/tool/tools/load_skill.py`:
- `class LoadSkillTool(Tool)`：
  - `name() -> "LoadSkill"`
  - `description()` → 描述按需激活 Skill 的语义
  - `input_schema()` → `{"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}`
  - `is_read_only() -> True`
  - `is_destructive() -> False`
  - `is_concurrency_safe() -> False`
  - `category() -> "skill"`
  - `is_system_tool = True`
  - 持有 `_loader: SkillLoader | None` 和 `_agent: Agent | None`
  - `set_loader(loader)` / `set_agent(agent)` 注入
  - `async execute(context, input) -> ToolResult`：
    - 未注入 → `ToolResult(success=False, error="LoadSkill not initialized")`
    - `loader.get(input["name"])` 为 None → `ToolResult(success=False, error=f"Unknown skill: {name}. Use /skill list to see available skills.")`
    - `agent.activate_skill(name, skill.prompt_body)`
    - → `ToolResult(success=True, data=f"Skill '{name}' activated. SOP pinned to environment context.")`

`core/tool/tools/install_skill.py`:
- `class InstallSkillTool(Tool)`：
  - `name() -> "InstallSkill"`
  - `category() -> "skill"`
  - `is_read_only() -> False`（写盘 + 网络）
  - `is_system_tool = False`
  - 持有 `_catalog: SkillLoader`、`_work_dir: Path`、`_on_installed: Callable | None`
  - `async execute(context, input) -> ToolResult`：委托 `core/skills/install.py:install_from_url`

**影响文件**：`core/tool/tools/load_skill.py`、`core/tool/tools/install_skill.py`
**依赖任务**：T2、T6

## T8: 远程安装（install_from_url）

**说明**：`core/skills/install.py`（新建）

- `parse_skill_url(url: str) -> tuple[str, str]`：支持三种 URL 格式 → 返回 `(owner_repo, subpath)`
  - `https://skills.sh/<owner>/<repo>` → 取 release zip
  - `https://github.com/<owner>/<repo>/tree/<ref>/<path>` → GitHub Contents API
  - `https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>/SKILL.md` → 单文件下载
- `async install_from_url(url: str, install_root: Path) -> str`：
  1. 解析 URL，确定下载策略
  2. 下载到临时目录（httpx.AsyncClient，限时 60s、限单文件 1 MiB、限总大小 8 MiB、限文件数 64、限深度 4）
  3. 验证含 `SKILL.md`
  4. atomic rename 到 `install_root / skill_name /`
  5. 返回 skill_name
- zip-slip 防护：校验所有路径无 `..`、无绝对路径
- `InstallSkillTool.execute` 成功后调 `_on_installed` 回调 → `catalog.load_all()` → `register_skills_as_commands` 重建

**影响文件**：`core/skills/install.py`、`core/tool/tools/install_skill.py`
**依赖任务**：T2、T7

## T9: 命令注册（skill → /<name> + /skill 管理）

**说明**：新建 `core/commands/skill_register.py` + `core/commands/builtin_skill.py`；修改 `core/commands/builtins.py` + `core/commands/ui.py`

`core/commands/ui.py` — UI 协议新增方法：
- `list_catalog_skills() -> list[dict]`：每条含 name/description/source/mode
- `list_active_skills() -> list[str]`
- `clear_active_skills() -> None`
- `append_assistant_message(text: str) -> None`：fork 路径把子 Agent 结果写入主对话
- `NopUI` 提供零值实现

`core/commands/skill_register.py`:
- `register_skills_as_commands(reg: Registry, catalog: SkillLoader, executor: SkillExecutor)`：
  - 模块级 `_REGISTERED_SKILL_NAMES: set[str]` 跟踪
  - 再次调用时先调 `remove_skill_commands(reg)` 清旧命令
  - 遍历 `catalog.list_all()`：
    - 如果 name 与已有内置命令冲突 → `logger.warning` 跳过
    - 注册 `Command(name=skill.meta.name, description=f"{skill.meta.description} [skill]", kind=Kind.PROMPT, handler=_make_skill_handler(skill, executor))`
  - `_make_skill_handler`：inline → `executor.execute_inline` + `ui.inject_and_send`；fork → `asyncio.create_task(_run_fork)` → `ui.append_assistant_message`
  - 闭包循环变量用默认参数 `_name=skill.meta.name` 捕获

`core/commands/builtin_skill.py`:
- `/skill list` → 遍历 catalog 输出 `  {name:<20} {desc}  [{source}]`
- `/skill info <name>` → 显示完整 frontmatter + 源路径 + 是否目录型
- `/skill reload` → `catalog.load_all()` → `register_skills_as_commands` 重建

`core/commands/builtins.py`:
- 移除 `/review` 的硬编码注册（改为 Skill 提供）
- `register_builtins` 新增 `register_skill_cmd(reg)` 注册 `/skill` 命令
- 或者 `/review` handler 改为委托 Skill 执行器

`core/commands/builtin_ui.py`:
- `handle_clear` 追加 `ui.clear_active_skills()`

**影响文件**：`core/commands/skill_register.py`、`core/commands/builtin_skill.py`、`core/commands/builtins.py`、`core/commands/builtin_ui.py`、`core/commands/ui.py`
**依赖任务**：T5、T6

## T10: 三个内置 Skill（commit / review / test）

**说明**：在项目级 `.codeforge/skills/` 下创建三个内置 Skill 目录

`.codeforge/skills/commit/SKILL.md`:
```yaml
---
name: commit
description: 分析 git diff 生成 commit message 并提交
allowed_tools: [Bash, Read, Glob, Grep]
mode: inline
---
```
SOP 正文：先跑 `git diff --staged` 和 `git diff` 收集变更 → 分析变更生成规范的 commit message → 确认后执行 `git commit`

`.codeforge/skills/review/SKILL.md`:
```yaml
---
name: review
description: 审查代码变更指出潜在 bug 和可简化处
allowed_tools: [Bash, Read, Glob, Grep]
mode: fork
fork_context: full
---
```
SOP 正文：审查当前上下文中的代码变更/已读取文件，指出潜在 bug、可读性问题和可简化处。替换原有 `/review` 命令。

`.codeforge/skills/test/SKILL.md`:
```yaml
---
name: test
description: 运行测试并修复失败或补写缺失测试
allowed_tools: [Bash, Read, Glob, Grep, Write, Edit]
mode: inline
---
```
SOP 正文：先找已有测试 → 有则运行 → 失败则分析原因修复代码；无测试则分析项目结构补写，然后运行确认通过。

**影响文件**：`.codeforge/skills/commit/SKILL.md`、`.codeforge/skills/review/SKILL.md`、`.codeforge/skills/test/SKILL.md`
**依赖任务**：T9

## T11: 接入主流程（tui/app.py）

**说明**：修改 `tui/app.py` + `core/skills/__init__.py`

`core/skills/__init__.py`:
- 导出 `SkillLoader, SkillExecutor, SkillParseError, SkillDependencyError, ActiveSkills, render_body, SkillMeta, SkillDef, SkillSource, ActiveEntry`

`tui/app.py` — `_run_async` 启动期接线：
1. 在构造 `agent` 之前：
   - `skill_loader = SkillLoader(workspace); skill_loader.load_all()`
   - `load_skill_tool = LoadSkillTool(); load_skill_tool.set_loader(skill_loader)`
   - `install_skill_tool = InstallSkillTool(skill_loader, workspace)`
2. 构造 `registry` 后立即 `registry.register(load_skill_tool); registry.register(install_skill_tool)`
3. `skill_loader.validate_tools(registry)` — 每个 Skill 的 `allowed_tools` 中存在不存在的工具 → 打 warning 并从 catalog 移除该 Skill
4. 构造 Agent 后 `load_skill_tool.set_agent(agent)`
5. `skill_executor = SkillExecutor(skill_loader, runtime, registry, provider, workspace)`
6. `agent.set_skill_catalog(_build_skill_catalog_text(skill_loader))`
7. `register_skills_as_commands(cmd_reg, skill_loader, skill_executor)`
8. `CodeForgeApp` 新增字段 `skill_loader` / `skill_executor`
9. `CodeForgeApp` 实现新增 UI 方法：`list_catalog_skills` / `list_active_skills` / `clear_active_skills` / `append_assistant_message`
10. `clear_and_new_session` 追加 `self.runtime.active_skills.clear()`

**影响文件**：`tui/app.py`、`core/skills/__init__.py`、`core/commands/__init__.py`
**依赖任务**：T1-T10 全部

## T12: 测试 + 端到端验证

**说明**：新建 `tests/test_skills.py`

覆盖：
- parser：valid / missing frontmatter / invalid yaml / missing name / invalid name / invalid mode fallback
- loader：项目加载 / 用户加载 / 项目覆盖用户 / get 热重载 / 热重载失败回退 / 解析失败跳过 / reload
- render：with $ARGUMENTS / no placeholder / multiple placeholders / allowed_tools 提示
- filter_tool_registry：empty allowed / 过滤 / 系统工具透传 / 缺工具抛错
- LoadSkill：load existing / load unknown / 未初始化
- ActiveSkills：activate / duplicate activate updates / clear / snapshot
- Agent 集成：activate_skill 后 env 含 SOP / clear_active_skills 后 env 不含

端到端（手动 TUI）：
1. 启动 → `/help` 列出 3 个内置 Skill 命令
2. `/review` → fork 执行，子 Agent 结果回流到主对话
3. 编辑 `SKILL.md` 改一行 → 不重启再 `/review` → 新行生效
4. 自然语言 "review my code" → Agent 调 LoadSkill → env 出现 SOP
5. `/clear` → 再输入消息 → env 不再含旧 SOP
6. `/skill list` → 列出所有 Skill 含 source
7. `/skill info commit` → 显示完整 frontmatter

**影响文件**：`tests/test_skills.py`
**依赖任务**：T1-T11 全部

---

## 执行顺序

```
T1 → (T2, T3 并行) → T4（并行于 T2/T3）→ T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12
```

T4 可与 T1-T3 并行做（不依赖 Skill 数据结构）。
