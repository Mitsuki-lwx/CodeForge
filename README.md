# CodeForge

**CodeForge** is a terminal AI coding assistant written entirely in Python (Claude Code-style).

It features a pluggable ReAct agent loop, an extensible tool system, multi-agent team orchestration with fork sub-agents, two-level context compression with deterministic memory dedup, a multi-provider / multi-protocol LLM layer (prompt caching across both Anthropic- and OpenAI-style wire formats), tier-based model routing with runtime `/model` switching, and OpenTelemetry + Langfuse–based observability and evaluation.

It can also **verify its own work**, not just write it: start a dev server in the
background, drive a real headless browser at it, read the console and the accessibility
tree, assert on the DOM, and diff screenshots before/after an interaction to prove the
click actually did something. That loop is what separates "the code looks right" from
"the code was checked".

Built for **extensibility** (pluggable `AgentLoop`, config-level routing and hooks) and **observability** (per-sub-agent span attribution), with minimal-invasive instrumentation.

---

## Features

### Agent loop
- **Pluggable ReAct loop** — the default `ReactLoop` can be swapped for a custom loop via config (`loop:`) or the `--loop` CLI flag.
- **Phase state machine** — each run transitions `idle → running → idle`; `running` is observable to the TUI and hooks.
- **Turn-end reasons** — tracks `completed` / `max-tokens` / `blocked`; a `max-tokens` stop is sticky so the next turn continues seamlessly.
- **`pre_step` hook** — interception point fired before every LLM iteration (agent-loop level).

### Tool system

Built-in tools (15): `read_file` `write_file` `edit_file` `glob` `grep`
`bash` `bash_output` `kill_shell` `web_fetch` `web_search`
`browser_snapshot` `browser_eval` `browser_screenshot` `browser_diff` `ExitPlanMode`
— plus `state_tool` (goal / todo / constraint), `Agent` (delegation), the skill tools
and the team/task tools, for **27 tools** in a fully assembled session.

#### Verification loop

The point of the next three groups is closing the loop from "I wrote it" to "I checked it":

```
bash(run_in_background) → bash_output → browser_snapshot / browser_eval / browser_diff → kill_shell
     起服务                   读日志            看它真的渲染成什么样、交互真的生效了吗        收工
```

- **Background shells** — `bash(run_in_background=true)` starts a long-running
  process (dev server, watcher, build) and returns a `shell_id` immediately;
  `bash_output` polls its output, `kill_shell` stops it. The whole process **tree**
  is terminated (a bare `kill` leaves orphans on Windows — measured), and everything
  is reaped when the session closes. `bash_output` is read-only, so polling does not
  cost an approval.
- **Browser** — `browser_snapshot` opens a URL in real headless Chromium and reports
  the title, **visible text, console output and page errors** (pass `js` to
  click/interact first); `browser_eval` runs a JS expression for assertions
  (`document.querySelectorAll('.todo-item').length`); `browser_screenshot` saves a PNG;
  `browser_diff` screenshots before/after an interaction and reports **how many pixels
  changed** — `0` means the click had no visible effect, a bug that logs and DOM checks
  happily hide. Needs `pip install "codeforge[browser]"`.
  - No 178MB Chromium download required: the tools fall back to the system
    Edge/Chrome automatically (override with `CODEFORGE_BROWSER_CHANNEL`).
  - These tools can reach `localhost` — that is the point (verifying your own dev
    server) — so they are gated at the `command` permission level.
- **Web** — `web_fetch` (URL → readable text, HTML converted) and `web_search`
  (DuckDuckGo HTML by default; override with `CODEFORGE_SEARCH_URL`, or plug a keyed
  provider in via MCP). Both go through an SSRF guard: only public http/https is
  reachable, redirects are checked hop-by-hop, and loopback / private /
  cloud-metadata ranges are refused (`CODEFORGE_WEB_ALLOW_PRIVATE=1` opts out).
  Gated at `command` too — an outbound request is not "reading a file".
- **Vision (image feedback)** — with `vision: true` on the provider,
  `browser_screenshot` also feeds the image back to the model (Anthropic gets an image
  block inside `tool_result`; OpenAI gets a follow-up user message, because `role=tool`
  is text-only). **Off by default** — text-only models reject image blocks outright
  (measured). When off, the screenshot is still written to disk and the path is
  reported: you can look at it, the model can't.

#### Delegation & extension

- **Sub-agent delegation** (`agent_tool`) — foreground sync `await` for fast results;
  background tasks notify and auto-inject results into the main conversation. A
  foreground sub-agent is cancelled when the parent is (Ctrl-C reaches it), but is
  deliberately **not** put behind a timeout — see the note in
  `core/tool/tools/agent_tool.py`.
- **MCP support** — a pool of stdio/HTTP MCP servers exposing external tools
  (`/mcp` lists them). Three safety rules:
  - **Project config is not trusted by default.** `.codeforge/mcp.json` comes *with the
    repository*, and an MCP stdio server is "spawn a subprocess" — loading it by default
    means `git clone <any repo> && codeforge` runs that repo's command at startup, before
    you type anything, with your environment inherited. Only `~/.codeforge/mcp.json`
    (yours) is loaded; enable `features.mcp.trust_project: true` (or
    `CODEFORGE_TRUST_PROJECT_MCP=1`) to opt in. When skipped, startup says so.
  - **Subprocesses get a minimal environment** (PATH/TEMP/… only), not `os.environ`.
    Extra variables must be declared explicitly in the server's `env`, which supports
    `${VAR}` expansion — so "which secret goes to which server" is a decision you wrote down.
  - **Tool permissions follow the server's own `annotations.readOnlyHint`**; tools that
    don't declare it require approval (they are not auto-allowed). This is what makes a
    25-tool server like `@playwright/mcp` usable: its read-only tools run freely, the
    rest go through approval.

  One command installs a safe-defaulted server (currently `playwright`):

  ```bash
  codeforge mcp add playwright     # writes ~/.codeforge/mcp.json, prints every flag and why
  ```

  The template pins the decisions you would otherwise get wrong: `--headless`,
  `--isolated`, `--output-dir .codeforge/screenshots`, `--image-responses omit` for
  text-only models, and `--executable-path <detected browser>` — the last one because
  `--browser msedge` fails when Edge lives under `Program Files (x86)` (measured; the
  Node-side playwright only checks the x64 path). It never adds `--no-sandbox` or
  `--allow-unrestricted-file-access`.

### Multi-agent orchestration
- **Teams** — lead + members with a mailbox/registry backend, file-lock safety, and spawn/resume.
- **Fork sub-agents** — creation-time tool decision (bypass + whitelist), read-only forked messages.
- **Coordinator** — decomposes tasks and dispatches to workers.
- **Worktrees** — isolated git worktrees per member (`/worktree`).
- **Per-sub-agent span attribution** — `codeforge.agent.id` / `codeforge.agent.name` on every span.

### Context & memory
- **Two-level context compression** — Layer 1 spills tool results to disk; Layer 2 summarizes via the LLM (`/compact`).
- **Deterministic memory dedup** — `upsert_note` overwrites instead of duplicating; title normalization + similarity matching.
- **Session state** — goals, todos, and hard constraints with promote-to-project/user persistence (`/goal`, `/todo`, `/constraint`).
- **Note types** — `4 + 3` note kinds covering memory and session state.

### LLM layer
- **Multi-provider / multi-protocol** — Anthropic-style and OpenAI-style wire formats behind a single session interface.
- **Prompt caching** — `cache_control` breakpoints on both protocol families (verified `cache_read > 0` on DeepSeek endpoints).
- **Retry & backoff** — retries on `429/500/502/503/504`, no retry after a 200 stream begins.
- **Model routing** — tier-based (`cheap`) routing, **off by default**, with a custom `judge_prompt` escape hatch.
- **Runtime `/model` switch** — change the main model mid-conversation, keeping the conversation intact.

### Observability & evaluation
- **OpenTelemetry** — logs, metrics, and traces (three pillars); no-op when unconfigured.
- **Langfuse-first** — traces/observations export to Langfuse OTLP; local JSONL is the fallback.
- **Benchmark harness** — `codeforge-bench` with datasets, semantic/conformance/efficiency/quality evaluators.

### Extensibility & safety
- **Hooks** — `pre_tool` and `pre_step` interception with fail-open semantics.
- **Skills** — built-in skills (`commit`, `review`, `test`) plus runtime skill install/load.

### Permissions & safety

Everything the agent can do goes through one pipeline
(`core/permissions/checker.py`), in this order:

```
plan-mode whitelist → read path sandbox → safe-command allowlist → dangerous-command
blacklist → rule engine → permission mode → HITL
```

- **Modes** — `default` (reads free, writes/commands ask), `acceptEdits`, `plan`,
  `bypassPermissions`; switch with `/permission`, `/plan`, `/do`.
- **Rules** — project (`.codeforge/settings.json`) and user (`~/.codeforge/settings.json`)
  `allow` / `deny` / `ask` rules. The TUI's "allow and save" writes into the project file.
- **Unattended runs** — `codeforge host` and `codeforge --task` have no human to answer
  `ask`, so a policy answers instead. **`deny_all` is the default** (read-only): a headless
  run will not write files or run commands unless you pass
  `--unattended-policy allow_write` / `review` / `allow_all`.
- **Path sandbox** — reads *and* writes are checked against the workspace root;
  out-of-workspace reads raise a prompt rather than being silently allowed.
- **No retries on side effects** — `bash`, `write_file`, `edit_file` and the shell/browser
  tools set `max_retries = 0`. The registry retries on timeout by default, which would
  re-run a timed-out `git push` or `rm -rf`.
- **Redaction** — trace/observation output is scrubbed of vendor-shaped credentials on
  the way to disk (not in the UI, and not outbound to the model).

**What is *not* there, stated plainly:** there is **no OS-level sandbox**. `bash` runs
directly on your machine with your environment and your network access. The controls
above are an allowlist/denylist plus approval — and any such list is incomplete by
construction. The browser and web tools can reach your intranet (that is what makes them
useful for verifying a local dev server). Treat the permission layer as guardrails for a
cooperative agent, not as isolation from a hostile one. Container / landlock / Job Object
isolation is the missing piece.

---

## Installation

Requires **Python 3.11+**.

```bash
git clone https://github.com/Mitsuki-lwx/CodeForge.git
cd CodeForge
pip install -e .
```

Optional extras:

```bash
pip install -e ".[dev]"       # pytest / ruff / mypy —— 开发与 CI
pip install -e ".[browser]"   # playwright —— 浏览器验证工具
```

`browser_*` tools need a browser engine. You do **not** have to download the 178MB
bundled Chromium: on Windows the tools fall back to the system Edge/Chrome
automatically. To pick one explicitly, set `CODEFORGE_BROWSER_CHANNEL` (e.g. `msedge`),
or install the bundled one with `playwright install chromium`.

This installs two entry points:

- `codeforge` — the interactive TUI client.
- `codeforge-bench` — the benchmark/evaluation harness.

---

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your provider API key(s):

```bash
cp config.example.yaml config.yaml
```

`config.example.yaml` is the **annotated** reference (every field, why it exists, and
its default) — this README does not duplicate it, so the two cannot drift. The knobs
worth knowing about up front:

| Key | Default | What it does |
| --- | --- | --- |
| `providers[].api_key` | — | required |
| `providers[].vision` | `false` | model accepts **image input**; enables screenshot feedback |
| `providers[].model_aliases` | `{}` | `haiku`/`sonnet`/`opus` → real model names |
| `features.mcp.trust_project` | `false` | load the **repo's** `.codeforge/mcp.json` (see Permissions & safety) |
| `features.host.unattended_policy` | `deny_all` | what `codeforge host` does with `ask`-level tools |
| `features.router.enabled` | `false` | tier-based model routing |
| `loop` | `""` | custom agent-loop module path |

> **Security**: `config.yaml` contains your API keys and is gitignored — never commit it.

---

## CLI

```bash
codeforge                        # interactive TUI
codeforge --task "<prompt>"      # run a single task headlessly, then exit
codeforge --loop my.loop.module  # use a custom agent loop
codeforge --plan-mode            # start in plan mode
```

### Session host (detachable runs)

```bash
codeforge host                   # run a session in the foreground as a control server
codeforge runs                   # list runs (works even when no host is up)
codeforge attach <run_id>        # inspect a run; add --continue --prompt "…" to push a turn
```

The host listens on loopback only, with a per-launch token in
`<workspace>/.codeforge/host.token`, and defaults to `deny_all` for `ask`-level tools.
Killing the host leaves the run marked `interrupted` with the reason recorded;
`attach` still works offline against the on-disk record.

### MCP servers

```bash
codeforge mcp add playwright     # writes a safe-defaulted server into ~/.codeforge/mcp.json
```

See **Permissions & safety** for why the project-level config is not trusted by default.

### Headless autonomy (`--task`)

A headless run has no human to answer permission prompts, so `ask`-level tool
decisions are answered by an **unattended policy** instead. The default is the
most conservative one:

| `--unattended-policy` | reads | writes files | runs commands |
| --- | --- | --- | --- |
| `deny_all` (default for `--task`) | ✅ | ❌ | ❌ |
| `allow_write` | ✅ | ✅ | ❌ |
| `review` | ✅ | judged per-call by a reviewer model | judged per-call |
| `allow_all` | ✅ | ✅ | ✅ |

```bash
codeforge --task "fix the failing tests" --unattended-policy allow_write
```

Dangerous commands (blacklist) and out-of-workspace writes are denied in **every**
mode, including `allow_all`. `codeforge host` reads its policy from
`features.host.unattended_policy` (also defaulting to `deny_all`).

`--team-member`, `--team`, `--member`, `--agent-id`, `--session-dir`, `--worktree`, `--agent-type`, `--model` are used internally by the team system when spawning sub-processes.

---

## Built-in commands

| Command | Alias | Description |
| --- | --- | --- |
| `/help` | `h` | List available commands |
| `/status` | `stats` | Mode / tokens / tools / memory / model / directory |
| `/model` | | Switch the main model at runtime |
| `/mcp` | | List MCP servers and their tools |
| `/observability` | `obs` | Metrics / logs / trace summary |
| `/memory` | `notes` | Loaded memory files |
| `/session` | `sessions` | Current session info |
| `/permission` | `mode` | Current permission mode |
| `/hooks` | | Loaded hooks |
| `/agents` | | List / inspect / message / stop sub-agents |
| `/show` | | Expand a transcript turn (`--tail N`, `--tools`, `--cost`) |
| `/plan` | | Enter plan mode |
| `/do` | | Back to execution mode and run the plan |
| `/compact` | `compress` | Manual context compression |
| `/resume` | | Resume a historical session |
| `/clear` | | End session and start a new one |
| `/goal` | | View/set the session goal |
| `/todo` | | View/add/toggle session todos |
| `/constraint` | | View/add/promote hard constraints |
| `/worktree` | | Manage isolated worktrees |
| `/team` | | Manage teams |
| `/exit` | `quit` | Quit CodeForge |

`/skill` (query/execute skills) and `/review` are provided by the skill system and registered at runtime.

---

## Project layout

Flat layout: the packages live at the repository root (there is no enclosing
`codeforge/` package directory). `pyproject.toml` declares the packages explicitly
via `[tool.setuptools]`.

```
CodeForge/
├── main.py                 # entry point: config → provider → TUI (py-module)
├── config/                 # YAML loader + Provider/Router/Features models
├── llm/                    # transport / adapters / session (retry, prompt cache)
├── core/
│   ├── agent/              # React loop, sub-agents, fork, router, builtin agents
│   │   ├── agent.py        # Agent：循环编排 + 状态（其余按关注点拆到下面几个文件）
│   │   ├── agent_telemetry.py    # trace / metrics / span
│   │   ├── agent_compaction.py   # 上下文压缩
│   │   ├── agent_permissions.py  # 权限判定与 HITL 代答
│   │   ├── agent_tools.py        # 工具执行与结果回灌
│   │   ├── agent_state.py        # 会话状态 / 记忆 / hook / 模型切换
│   │   └── tool_category.py      # 工具 category → 权限类别（纯函数）
│   ├── tool/               # tool registry + built-in tools
│   │   ├── shells.py       # background shell registry (process-tree kill + reaping)
│   │   ├── web.py          # HTTP fetch + HTML→text + SSRF guard
│   │   └── browser.py      # headless Chromium: snapshot / eval / screenshot / pixel diff
│   ├── team/               # lead/member, mailbox, registry, tasks, backend
│   ├── coordinator/        # task decomposition & dispatch
│   ├── task/               # task model
│   ├── worktree/           # isolated git worktrees
│   ├── context_compression/# Layer 1 (spill) + Layer 2 (summarize)
│   ├── notes/              # note store + session state (deterministic dedup)
│   ├── archive/            # session persistence & restore
│   ├── commands/           # slash-command registry + builtins
│   ├── skills/             # skill loader/executor
│   ├── hooks/              # pre_tool / pre_step interception
│   ├── permissions/        # permission modes + rule engine + path sandbox
│   ├── mcp/                # MCP pool + stdio/http transports
│   │   └── templates.py    # safe-defaulted server templates (`mcp add <name>`)
│   ├── observability/      # OTel logs/metrics/traces, Langfuse export
│   └── trace/              # audit traces
├── conversation/           # conversation manager
├── tui/                    # prompt_toolkit + Rich terminal UI
│   ├── app.py              # CodeForgeApp 状态容器 + 命令实现
│   ├── session.py          # 会话编排：run / _run_async / 事件消费 / HITL 应答
│   ├── render.py           # 横幅 / 流式渲染 / 工具标题 / spinner（纯渲染件）
│   ├── app_inspect.py      # `/agents`、`/show`
│   ├── app_skills.py       # `/skill`、`/hooks` 视图
│   └── app_workspace.py    # `/worktree`、`/team`
├── benchmark/              # evaluation harness (`codeforge-bench`)
├── .github/workflows/      # CI: ubuntu+windows × py3.11/3.12, ruff + mypy + install smoke
└── tests/                  # pytest suite
```

---

## Development

```bash
pip install -e ".[dev]"       # 运行时依赖 + pytest / pytest-asyncio / ruff / mypy
python -m pytest              # 运行时行为验证（asyncio auto mode）
ruff check                    # lint
ruff format --check           # 格式检查（`ruff format` 就地重排）
mypy                          # 静态类型检查（白名单，见 pyproject）
```

四件套的分工：**pytest 验行为、ruff check 验写法、ruff format 统一格式、
mypy 验类型**。全部接进 CI，四个平台组合都会跑。

`mypy` 是**增量白名单**（`files` 列在 `pyproject.toml` 里，当前 111 个文件），
不是"全库类型已达标"：存量还有约 130 条，新文件加进白名单前必须先修到 0。
`ruff` 只开能抓到真问题的规则集（`E4/E7/E9` `F` `B` `I` `UP`），刻意不开 `E501`
与 `ARG` —— 理由写在 `pyproject.toml` 的注释里。

CI (`.github/workflows/ci.yml`) runs the suite on **ubuntu-latest + windows-latest**
× **Python 3.11 / 3.12**, and additionally smoke-tests that the package installs and
that both entry points (`codeforge`, `codeforge-bench`) respond to `--help` —
installation was broken once, so it is checked on every push.

A few tests need a shell: they use the interpreter itself (`sys.executable`) or skip
when a tool is absent, so they pass on both platforms without depending on Git's
Unix utilities being on `PATH`.

---

## License

No license file yet — all rights reserved by default. (Personal learning project.)
