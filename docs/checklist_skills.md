# Checklist — Skill 系统

每一项通过运行代码或观察行为验证。条目格式：操作 → 预期可观测结果。

---

## 1. 实现完整性

### 1.1 解析与类型

- [x] `ls core/skills/` 列出 types.py / parser.py / loader.py / active.py / render.py / executor.py / install.py / adapter.py / __init__.py
- [x] `python -c "from core.skills import SkillLoader, SkillExecutor, SkillMeta, SkillDef, SkillSource, ActiveEntry, ActiveSkills, SkillParseError, SkillDependencyError"` 退出码 0
- [x] `grep -n "class SkillMeta" core/skills/types.py` 命中，字段含 name / description / allowed_tools / mode / fork_context / model
- [x] `grep -n "class SkillDef" core/skills/types.py` 命中，字段含 meta / prompt_body / source_path / source / is_directory
- [x] `grep -n "class ActiveEntry" core/skills/types.py` 命中，字段含 name / body
- [x] `grep -n "parse_skill_file" core/skills/parser.py` 命中
- [x] `grep -n "SkillParseError" core/skills/parser.py` 命中
- [x] `grep -n "\\$ARGUMENTS" core/skills/render.py` 命中（占位符替换逻辑）

### 1.2 Loader

- [x] `grep -n "PROJECT_SKILLS_DIR\|USER_SKILLS_DIR" core/skills/loader.py` 命中 ≥2 处
- [x] `grep -n "def load_all" core/skills/loader.py` 命中
- [x] `grep -n "def get" core/skills/loader.py` 命中（热重载：每次重读源文件）
- [x] `grep -n "_cache" core/skills/loader.py` 命中（热重载失败回退）
- [x] `grep -n "get_source_label" core/skills/loader.py` 命中，返回 project/user
- [x] 项目级 Skill 覆盖同名用户级 Skill（`grep -n "project.*user\|先.*项目" core/skills/loader.py` 确认逻辑）

### 1.3 ActiveSkills

- [x] `grep -n "class ActiveSkills" core/skills/active.py` 命中
- [x] `grep -n "def activate\|def clear\|def snapshot\|def names" core/skills/active.py` 命中 ≥4 处
- [x] 重复 activate 同名 Skill → body 覆盖（不新增条目）

### 1.4 Executor

- [x] `grep -n "class SkillExecutor" core/skills/executor.py` 命中
- [x] `grep -n "execute_inline\|execute_fork" core/skills/executor.py` 命中 ≥2 处
- [x] `grep -n "SkillDependencyError" core/skills/executor.py` 命中
- [x] `grep -n "filter_tool_registry" core/skills/executor.py` 命中
- [x] `grep -n "SYSTEM_TOOL_NAMES" core/skills/executor.py` 命中，值为 `frozenset({"LoadSkill"})`

### 1.5 工具系统修改

- [x] `grep -n "is_system_tool" core/tool/interface.py` 命中（Tool ABC 新属性，默认 False）
- [x] `grep -n "definitions_filtered" core/tool/registry.py` 命中
- [x] `grep -n "system_definitions" core/tool/registry.py` 命中
- [x] `grep -n "is_system_tool = True" core/tool/tools/load_skill.py` 命中
- [x] `ls core/tool/tools/load_skill.py` 存在
- [x] `ls core/tool/tools/install_skill.py` 存在

### 1.6 Agent 集成

- [x] `grep -n "active_skills" core/agent/runtime.py` 命中（SessionRuntime 新字段）
- [x] `grep -n "active_skills.clear()" core/agent/runtime.py` 命中（reset_for_new_session 内）
- [x] `grep -n "activate_skill\|clear_active_skills\|set_skill_catalog" core/agent/agent.py` 命中 ≥3 处
- [x] `grep -n "active_skills" core/prompts/environment.py` 命中（collect_environment 新参数）

### 1.7 命令集成

- [x] `ls core/commands/skill_register.py` 存在
- [x] `ls core/commands/builtin_skill.py` 存在
- [x] `grep -n "register_skills_as_commands" core/commands/skill_register.py` 命中
- [x] `grep -n "remove_skill_commands" core/commands/skill_register.py` 命中
- [x] `grep -n "\\[skill\\]" core/commands/skill_register.py` 命中（描述末尾标记）
- [x] `grep -n "clear_active_skills" core/commands/builtin_ui.py` 命中（/clear 追加调用）
- [x] `grep -n "append_assistant_message\|list_catalog_skills\|list_active_skills\|clear_active_skills" core/commands/ui.py` 命中 ≥4 处
- [x] NopUI 实现全部新增 UI 方法（`grep -n "def list_catalog_skills\|def list_active_skills\|def clear_active_skills\|def append_assistant_message" core/commands/ui.py` 命中 ≥4 处）

### 1.8 远程安装

- [x] `ls core/skills/install.py` 存在
- [x] `grep -n "parse_skill_url" core/skills/install.py` 命中
- [x] `grep -n "install_from_url" core/skills/install.py` 命中
- [x] `grep -n "MAX_FILE_SIZE\|MAX_TOTAL_SIZE\|MAX_FILE_COUNT\|MAX_RECURSION_DEPTH" core/skills/install.py` 命中 ≥4 处限额常量
- [x] 限额值：单文件 ≤1 MiB / 总大小 ≤8 MiB / 文件数 ≤64 / 深度 ≤4

### 1.9 内置 Skill

- [x] `ls .codeforge/skills/commit/SKILL.md` 存在
- [x] `ls .codeforge/skills/review/SKILL.md` 存在
- [x] `ls .codeforge/skills/test/SKILL.md` 存在
- [x] commit SKILL.md frontmatter 含 `mode: inline`、`allowed_tools: [Bash, Read, Glob, Grep]`
- [x] review SKILL.md frontmatter 含 `mode: fork`、`fork_context: full`、`allowed_tools: [Bash, Read, Glob, Grep]`
- [x] test SKILL.md frontmatter 含 `mode: inline`、`allowed_tools` 含 write_file/edit_file

## 2. 接入完整性（杜绝死代码）

- [x] `grep -rn "SkillLoader" tui/app.py` 命中 ≥2 处（import + 实例化）
- [x] `grep -rn "SkillExecutor" tui/app.py` 命中 ≥2 处（import + 实例化）
- [x] `grep -rn "LoadSkillTool\|InstallSkillTool" tui/app.py` 命中 ≥2 处（import + 注册）
- [x] `grep -rn "register_skills_as_commands" tui/app.py` 命中
- [x] `grep -rn "set_skill_catalog" tui/app.py` 命中
- [x] `grep -rn "activate_skill" core/` 命中 Agent 方法定义 + SkillExecutor 调用 + LoadSkillTool 调用 ≥3 处
- [x] `grep -rn "clear_active_skills" core/` 命中 Agent 方法定义 + /clear handler + reset_for_new_session ≥3 处
- [x] `grep -rn "definitions_filtered" core/` 命中 registry 定义 + executor 调用 ≥2 处
- [x] `grep -rn "is_system_tool" core/` 命中 interface 定义 + executor 过滤 + LoadSkill 实现 ≥3 处
- [x] `CodeForgeApp` 含 `skill_loader` / `skill_executor` 字段（`grep -n "skill_loader\|skill_executor" tui/app.py` 命中）
- [x] `/review` 不再以硬编码 Command 注册（`grep -n '"review"' core/commands/builtins.py` 无命中或注释掉）
- [x] `.codeforge/skills/` 不做 ignore（`grep "codeforge/skills" .gitignore` 不命中 `*.md` ignore 模式）

## 3. 热重载与容错

- [x] 单个 Skill 解析失败 → `logging.warning` 输出含 `Skipping skill`（`grep -rn "Skipping" core/skills/loader.py` 命中）
- [x] 解析失败的 Skill 不阻断其他 Skill 加载（catalog 仍含其余有效 Skill）
- [x] 热重载：修改 SKILL.md 后不重启 /<name> 执行 → 新内容生效
- [x] 热重载失败：SKILL.md 被改成非法 yaml → 下次执行仍用旧缓存 + warning
- [ ] Skill 名与内置命令冲突 → 跳过加载 + warning（不覆盖内置命令行为）
- [x] 白名单含不存在的工具 → 启动期 `SkillDependencyError` 阻止启动

## 4. 编译与测试

- [x] `ruff check core/skills/ core/tool/tools/load_skill.py core/tool/tools/install_skill.py` 无 error
- [x] `ruff format --check core/skills/` 通过
- [x] `pytest tests/test_skills.py -v` 全部通过
- [x] `pytest tests/ -v` 全量通过（不破坏既有测试）
- [x] `python -c "from core.skills import SkillLoader; l = SkillLoader(Path.cwd()); l.load_all(); print([s.meta.name for s in l.list_all()])"` 列出 Skill 名不报错

## 5. 端到端验证（手动操作 TUI）

> 启动命令：`python main.py`

### A 启动与列表

- [ ] 启动 TUI，`/help` → 列出 `/commit [skill]`、`/review [skill]`、`/test [skill]`、`/skill` 四条命令
- [ ] `/skill list` → 列出 3 个内置 Skill，含 name / description / source（project）
- [ ] `/skill info review` → 显示完整 frontmatter（name/description/mode: fork/fork_context: full/allowed_tools） + 源路径

### B inline Skill（commit）

- [ ] `/commit` → Agent 执行 `git diff --staged` + `git diff` → 生成 commit message → 状态栏流式渲染 → 会话存档含 assistant 消息

### C fork Skill（review）

- [ ] `/review 注意安全` → 后台 fork 子 Agent 启动 → 结果以 assistant 消息写入主对话 → 内容为代码审查结论
- [ ] fork 子 Agent 的 token 用量计入主 SessionRuntime（`/status` Tokens 增加）

### D 意图触发

- [ ] 自然语言输入 "review my last change" → Agent 调 `LoadSkill({name: "review"})` → 下一轮 env 出现 review SOP → LoadSkill 调用**不弹权限提示**

### E 热重载

- [x] 编辑 `.codeforge/skills/commit/SKILL.md`，在 body 加一行 `## EXTRA: test hot reload` → 不重启 TUI → `/commit` → Agent 回复中体现新行内容
- [ ] 把 SKILL.md 改成非法 yaml → `/commit` 仍执行（回退旧缓存）+ 日志含 warning

### F `/clear` 联动

- [x] 先 `/commit` 激活 commit Skill → `/clear` → 输任意消息 → env 不再含 `## Active Skills`
- [ ] `/clear` 后旧会话 JSONL 保留（`ls .codeforge/sessions/` 数量 +1）

### G `/skill reload`

- [ ] 新建 `.codeforge/skills/hello/SKILL.md`（name: hello, mode: inline）→ `/skill reload` → `/help` 列出 `/hello [skill]` → `/hello` 可执行

### H 容错

- [x] 创建 `.codeforge/skills/bad/SKILL.md` 故意写错 frontmatter（无 name 字段）→ 重启 → 启动日志含 `Skipping` warning → `/skill list` 不含 bad 但其他 3 个 Skill 正常

### I 冲突

- [ ] 创建 `.codeforge/skills/status/SKILL.md`（与内置 /status 同名）→ 重启 → 日志 warning 含冲突信息 → `/status` 仍走内置命令

### J 工具白名单

- [ ] 创建 fork Skill 设 `allowed_tools: [bash, read_file, glob, grep]` → 执行 → 子 Agent 只有 Read/Glob/LoadSkill 三个工具可用
- [x] 创建 Skill 设 `allowed_tools: [NonExistentTool]` → 启动期 `SkillDependencyError` → 进程以非 0 退出

## 6. 文档

- [ ] `docs/spec_skills.md` 更新到最终版
- [x] `docs/tasks_skills.md` 12 个任务全部勾上
- [ ] `docs/checklist_skills.md` 全部条目勾上
