# CodeForge

CodeForge is a terminal AI coding assistant written entirely in Python (Claude Code-style).

It features a pluggable ReAct agent loop, an extensible tool system, multi-agent team orchestration with fork sub-agents, two-level context compression with deterministic memory dedup, a multi-provider/multi-protocol LLM layer (prompt caching across both Anthropic- and OpenAI-style wire formats), model routing with runtime `/model` switching, and OpenTelemetry + Langfuse–based observability and evaluation.

Built for extensibility (pluggable `AgentLoop`, config-level routing and hooks) and observability (per-sub-agent span attribution), with minimal-invasive instrumentation.

## Highlights

- **Pluggable ReAct agent loop** — swap the default React loop for a custom one via config or CLI flag.
- **Extensible tool system** — shell, file tools, sub-agents, and MCP tools behind a single registry.
- **Multi-agent orchestration** — teams, tasks, coordinator, worktrees, and fork sub-agents.
- **Context compression & memory** — two-level compression plus deterministic note dedup, session goals/todos/constraints.
- **Multi-protocol LLM layer** — Anthropic- and OpenAI-style wire formats, prompt caching, retry/backoff.
- **Model routing** — tier-based routing (default off) and runtime `/model` switching.
- **Observability & evaluation** — OpenTelemetry (logs/metrics/traces) and Langfuse-first scoring.

## Getting started

```bash
pip install -e .
codeforge
```

Copy `config.example.yaml` to `config.yaml` and fill in your provider API key(s).

> **Security**: `config.yaml` holds your API keys and is gitignored — never commit it.
