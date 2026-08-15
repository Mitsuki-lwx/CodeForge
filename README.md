# CodeForge

**CodeForge** is a terminal AI coding assistant written entirely in Python (Claude Code-style).

It features a pluggable ReAct agent loop, an extensible tool system, multi-agent team orchestration with fork sub-agents, two-level context compression with deterministic memory dedup, a multi-provider / multi-protocol LLM layer (prompt caching across both Anthropic- and OpenAI-style wire formats), tier-based model routing with runtime `/model` switching, and OpenTelemetry + Langfuse–based observability and evaluation.

Built for **extensibility** (pluggable `AgentLoop`, config-level routing and hooks) and **observability** (per-sub-agent span attribution), with minimal-invasive instrumentation.

---

## Features

### Agent loop
- **Pluggable ReAct loop** — the default `ReactLoop` can be swapped for a custom loop via config (`loop:`) or the `--loop` CLI flag.
- **Phase state machine** — each run transitions `idle → running → idle`; `running` is observable to the TUI and hooks.
- **Turn-end reasons** — tracks `completed` / `max-tokens` / `blocked`; a `max-tokens` stop is sticky so the next turn continues seamlessly.
- **`pre_step` hook** — interception point fired before every LLM iteration (agent-loop level).

### Tool system
- Built-in tools: `bash`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, plus `state_tool` (session goal/todo/constraint).
- **Sub-agent delegation** (`agent_tool`) — foreground sync `await` for fast results; background tasks notify and auto-inject results into the main conversation.
- **MCP support** — a pool of stdio/HTTP MCP servers exposing external tools (`/mcp` lists them).

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
- **Permissions** — mode-based permission system (`/permission`, `/plan`, `/do`).
- **Skills** — built-in skills (`commit`, `review`, `test`) plus runtime skill install/load.

---

## Installation

Requires **Python 3.11+**.

```bash
git clone https://github.com/Mitsuki-lwx/CodeForge.git
cd CodeForge
pip install -e .
```

This installs two entry points:

- `codeforge` — the interactive TUI client.
- `codeforge-bench` — the benchmark/evaluation harness.

---

## Configuration

Copy `config.example.yaml` to `config.yaml` and fill in your provider API key(s):

```bash
cp config.example.yaml config.yaml
```

Full example:

```yaml
providers:
  - name: "Anthropic"
    protocol: anthropic          # "anthropic" | "openai"
    model: "claude-sonnet-4-20250514"
    api_key: "sk-..."            # required
    # base_url: "https://..."    # optional, overrides protocol default
    # vendor: anthropic          # optional: anthropic | openai | deepseek
    # thinking: true             # extended thinking (anthropic protocol)
    # tier: cheap                # model-routing tier marker

  - name: "OpenAI"
    protocol: openai
    model: "gpt-4o"
    api_key: "sk-..."

# Observability — export to Langfuse OTLP (optional). Omit `endpoint` to keep logs local.
observability:
  endpoint: http://localhost:3000/api/public/otel/v1/traces
  headers:
    Authorization: Basic <base64(public-key:secret-key)>

# Feature flags
features:
  coordinator_mode: false
  fork_teammate: false
  router:
    enabled: false               # model routing, off by default
    cheap_tier: "cheap"
    judge_prompt: ""             # optional custom complexity judge

# Agent loop strategy: "react" (default) or a custom module path
loop: ""
```

> **Security**: `config.yaml` contains your API keys and is gitignored — never commit it. The example above uses placeholders, not real credentials.

---

## CLI

```bash
codeforge                        # interactive TUI
codeforge --task "<prompt>"      # run a single task headlessly, then exit
codeforge --loop my.loop.module  # use a custom agent loop
codeforge --plan-mode            # start in plan mode
```

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

```
codeforge/
├── main.py                 # entry point: config → provider → TUI
├── config/                 # YAML loader + Provider/Router/Features models
├── llm/                    # transport / adapters / session (retry, prompt cache)
├── core/
│   ├── agent/              # React loop, sub-agents, fork, router, builtin agents
│   ├── tool/               # tool registry + built-in tools
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
│   ├── permissions/        # permission modes
│   ├── mcp/                # MCP pool + stdio/http transports
│   ├── observability/      # OTel logs/metrics/traces, Langfuse export
│   └── trace/              # audit traces
├── conversation/           # conversation manager
├── tui/                    # prompt_toolkit + Rich terminal UI
├── benchmark/              # evaluation harness (`codeforge-bench`)
└── tests/                  # pytest suite
```

---

## Development

```bash
pytest                        # run the test suite (asyncio auto mode)
```

---

## License

Proprietary / all rights reserved (pending license choice).
