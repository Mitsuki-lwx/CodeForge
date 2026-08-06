# CodeForge Skill 系统 · 规格说明

## 背景

CodeForge 当前有 12 条写死在源码里的斜杠命令（`/review`、`/plan` 等），用户无法扩展。三个痛点：

1. **不可复用** — 同样的 SOP（commit message 规范、代码审查清单、跑测试流程）每次手敲或靠 `/review` 硬编码
2. **工具选择退化** — 工具一多（7 内置 + MCP 外部），模型选错工具的概率上升
3. **无任务级隔离** — 所有 prompt 共享同一对话上下文，特定任务需要独立的工具白名单和对话环境

Skill 把可复用 SOP 装进可编辑的 Markdown 文件，配渐进式披露、工具白名单与两种执行模式，同时解决上述三个问题。

## 目标用户

CodeForge 终端用户，通过编写 `.codeforge/skills/<name>/SKILL.md` 扩展自定义能力。

## 能力清单

1. **Markdown 文件即 Skill** — 单个 Skill 是含 YAML frontmatter + Markdown 正文的 `SKILL.md`，放在同名目录下；目录内可附带 references 资源
2. **两级路径加载** — 项目目录 `.codeforge/skills/` 优先级高于用户目录 `~/.codeforge/skills/`，同名项目级覆盖用户级
3. **渐进式披露（两阶段加载）** — 启动时只把 Skill 的 name + description 列表注入对话；Agent 按需调 `LoadSkill` 工具加载完整 SOP
4. **SOP 钉入环境上下文** — 激活后的 SOP 每轮对话重建 environment 时都注入，多个 Skill 可同时激活
5. **两种执行模式** — inline（共享主对话执行）和 fork（独立子会话执行后摘要回流）
6. **工具白名单** — 每个 Skill 声明 `allowed_tools` 列表收窄可用工具；白名单在现有权限链上叠加过滤
7. **自动注册斜杠命令** — 每个 Skill 自动注册为 `/<name>` 命令，描述末尾标 `[skill]`
8. **热重载** — 修改 `SKILL.md` 下次执行即生效，无需重启
9. **远程安装** — `InstallSkill` 工具支持从 GitHub URL 自动下载安装 Skill
10. **三个内置 Skill** — commit（生成 message 并提交）、review（替换现有 `/review`）、test（跑测试→修→补写）
11. **`/skill` 管理命令** — 列出已加载 Skill、查看详情、热重载目录
12. **`/clear` 联动清理** — 清空对话时同步清空已激活的 Skill

## 非功能要求

- **N1**：单个 Skill 解析失败不阻断其他 Skill 加载，错误走 `logging.warning`
- **N2**：`LoadSkill` 工具调用不弹权限提示（`is_system_tool=True` + `category="skill"`）
- **N3**：fork 模式必须隔离 `ConversationManager`，主对话状态不被子 Agent 修改
- **N4**：工具白名单过滤通过 `ToolRegistry` 的过滤视图实现，不修改原注册表
- **N5**：Skill 与已有内置命令同名时，内置命令优先，Skill 跳过并打 warning
- **N6**：白名单中出现不存在的工具，启动时立即 `raise SkillDependencyError` 阻止启动
- **N7**：启动期加载 Skill 目录不阻塞 UI（I/O 在主 asyncio event loop 上同步完成，量小无感）
- **N8**：`$ARGUMENTS` 占位符在加载时由用户输入替换

## 设计骨架

### 核心数据结构

```
core/skills/
├── __init__.py          # 导出
├── types.py             # SkillMeta, SkillDef, SkillSource, ActiveEntry
├── parser.py            # 解析 SKILL.md frontmatter + body
├── loader.py            # SkillLoader 两级路径扫描 + 热重载
├── active.py            # ActiveSkills 跨轮激活列表
├── render.py            # render_body（$ARGUMENTS 替换 + 工具提示）
├── executor.py          # SkillExecutor（inline / fork 分发）
└── install.py           # install_from_url（GitHub API + zip 防护）
```

**SkillMeta**（`core/skills/types.py`）：
- `name: str` — 唯一标识，小写字母数字连字符
- `description: str` — 一句话说明
- `allowed_tools: list[str]` — 可见工具白名单（空 = 不过滤）
- `mode: Literal["inline", "fork"]` — 执行模式，默认 inline
- `fork_context: Literal["none", "recent", "full"]` — fork 模式携带历史量，默认 none
- `model: str | None` — 可选指定模型

**SkillDef**（`core/skills/types.py`）：
- `meta: SkillMeta`
- `prompt_body: str` — SKILL.md 去 frontmatter 后的正文
- `source_path: Path` — 源目录绝对路径
- `source: SkillSource` — USER / PROJECT
- `is_directory: bool` — 是否为目录型 Skill

**ActiveEntry**（`core/skills/types.py`）：
- `name: str`
- `body: str` — 激活那一刻从磁盘读取的正文

### 模块交互

```
启动: _run_async()
  ├─ SkillLoader(workspace).load_all()
  ├─ 构造 LoadSkill(catalog, active_skills) → registry.register()
  ├─ 构造 InstallSkill(catalog, workspace) → registry.register()
  ├─ catalog.validate_tools(registry) → fail-fast
  ├─ SkillExecutor(catalog, runtime, registry, provider)
  ├─ agent.set_skill_catalog(catalog.to_prompt_items())
  └─ register_skills_as_commands(cmd_reg, catalog, executor)

运行时（显式 /<name>）:
  user → /<name> → command handler → executor.execute(name, args)
    ├─ inline: render_body → agent.activate_skill → inject_and_send
    └─ fork: render_body → 子 Agent → final_text → append_assistant_message

运行时（意图触发）:
  Agent 调 LoadSkill({name}) → loader.get → agent.activate_skill
  → 下一轮 env 包含 SOP

/clear:
  handle_clear → clear_and_new_session → agent.clear_active_skills()
```

### 与现有模块的关系

- **`core/tool/interface.py`** — Tool ABC 新增 `is_system_tool` 属性（默认 False）
- **`core/tool/registry.py`** — 新增 `definitions_filtered(allowed)` 返回过滤视图
- **`core/agent/runtime.py`** — SessionRuntime 新增 `active_skills: ActiveSkills` 字段；`reset_for_new_session` 同步清空
- **`core/agent/agent.py`** — 新增 `activate_skill` / `clear_active_skills` / `set_skill_catalog`；每轮 env 拼接 active SOP
- **`core/prompts/modules.py`** — `_ACTIVE_SKILLS` 槽位改名为 skills-catalog，承载 name+description 列表
- **`core/prompts/environment.py`** — 扩展 `collect_environment` 输出，追加 active skills SOP 块（非缓存）
- **`core/commands/builtins.py`** — `/review` 替换为 review Skill；`/clear` handle 追加清理激活 Skill
- **`core/commands/`** — 新增 `builtin_skill.py`（`/skill` 管理命令）和 `skill_register.py`（自动注册 skill 为斜杠命令）
- **`core/commands/ui.py`** — UI 协议新增 `list_catalog_skills` / `list_active_skills` / `clear_active_skills` / `append_assistant_message`
- **`tui/app.py`** — CodeForgeApp 实现新 UI 方法；启动期接入 Skill 加载

## Out of Scope

- **Skill 市场/分发平台** — 不做 centralized registry、评分、搜索
- **Skill 版本管理** — 不做 semver、lockfile、依赖声明
- **MCP resources / prompts** — Skill 只做 tools，不做 MCP 其他能力
- **Skill 间依赖** — 一个 Skill 不声明依赖另一个 Skill
- **动态参数（runtime prompt injection）** — 不做 Skill 运行时由模型动态填充参数，只做加载时 `$ARGUMENTS` 替换
- **Skill 权限控制** — 不做 Skill 级别的独立权限配置
- **GUI Skill 编辑器** — 用户用任意编辑器写 Markdown
