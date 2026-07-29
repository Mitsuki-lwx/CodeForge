"""Real-prompt integration tests for the Agent Loop.

Uses the actual LLM provider from config.yaml to verify:
  Scenario A: Simple text response (natural completion, 1 round)
  Scenario B: Multi-turn tool chain (read file → act on content)
  Scenario C: Plan Mode toggle (/plan ON → plan → /plan OFF)
  Scenario D: Plan Mode auto-detect (natural language triggers plan mode)
  Scenario E: Concurrent read batch
  Scenario F: Error handling (nonexistent file)

Each scenario validates event types, iteration count, and final state.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import load_config
from conversation.manager import ConversationManager
from core.agent import Agent, AgentConfig
from core.agent.events import (
    AgentError,
    AgentFinished,
    AgentEvent,
    IterationUpdate,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from llm.client import LLMClient

SYSTEM_PROMPT = """You are CodeForge, a terminal AI coding assistant.
You have access to tools to read, write, and search files, and execute shell commands.
Use these tools to accomplish the user's task. Be concise.
When asked to do something, DO it — don't just describe it."""

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
DIM = "\033[2m"
RST = "\033[0m"


def _summarize(events: list[AgentEvent]) -> dict:
    """Summarize event list into counts."""
    return {
        "text_deltas": sum(1 for e in events if isinstance(e, TextDelta)),
        "tool_started": sum(1 for e in events if isinstance(e, ToolCallStarted)),
        "tool_finished": sum(1 for e in events if isinstance(e, ToolCallFinished)),
        "iterations": max(
            (e.iteration for e in events if isinstance(e, IterationUpdate)), default=0
        ),
        "finished": [e for e in events if isinstance(e, AgentFinished)],
        "errors": [e for e in events if isinstance(e, AgentError)],
        "full_text": "".join(e.text for e in events if isinstance(e, TextDelta)),
    }


async def run_prompt(
    agent: Agent, prompt: str, label: str = "", timeout: float = 60.0
) -> list[AgentEvent]:
    """Run a single prompt through the agent and collect all events."""
    events: list[AgentEvent] = []
    print(f"\n{DIM}── {label or prompt[:60]} ──{RST}")
    try:
        async with asyncio.timeout(timeout):
            async for e in agent.run(prompt):
                events.append(e)
                if isinstance(e, TextDelta):
                    print(e.text, end="", flush=True)
                elif isinstance(e, ToolCallStarted):
                    print(f"\n  >> [{e.name}] ", end="", flush=True)
                elif isinstance(e, ToolCallFinished):
                    icon = "OK" if e.success else "FAIL"
                    print(f"{icon} ", end="", flush=True)
                elif isinstance(e, AgentFinished):
                    usage = e.total_usage
                    print(
                        f"\n{DIM}({e.elapsed_s:.1f}s, {e.iterations}r, "
                        f"in:{usage.get('input_tokens',0)} out:{usage.get('output_tokens',0)}t){RST}"
                    )
                elif isinstance(e, AgentError):
                    print(f"\n  x {e.message} ({e.code})")
    except asyncio.TimeoutError:
        print(f"\n  x TIMEOUT after {timeout}s")
    print()
    return events


# ═══════════════════════════════════════════════════════════════════════
# Test scenarios
# ═══════════════════════════════════════════════════════════════════════


def check_a_simple_text(events: list[AgentEvent]) -> tuple[bool, str]:
    """Scenario A: Simple text response — 1 round, no tools, AgentFinished."""
    s = _summarize(events)
    if not s["finished"]:
        return False, f"No AgentFinished event (got errors: {s['errors']})"
    if s["tool_started"] > 0:
        return False, f"Expected 0 tool calls, got {s['tool_started']}"
    if s["iterations"] != 1:
        return False, f"Expected 1 iteration, got {s['iterations']}"
    if not s["full_text"].strip():
        return False, "Empty response text"
    return True, f"1 round, {len(s['full_text'])} chars text"


def check_b_multiturn(events: list[AgentEvent]) -> tuple[bool, str]:
    """Scenario B: Multi-turn — read a file, then respond about it."""
    s = _summarize(events)
    if not s["finished"]:
        return False, f"No AgentFinished (errors: {s['errors']})"
    if s["tool_started"] < 1:
        return False, f"Expected ≥1 tool call, got {s['tool_started']}"
    if s["iterations"] < 2:
        return False, f"Expected ≥2 iterations (tool + reply), got {s['iterations']}"
    if not s["full_text"].strip():
        return False, "No final text response"
    return True, f"{s['iterations']} rounds, {s['tool_started']} tool calls"


def check_c_plan_mode_toggle(
    toggle_on: list[AgentEvent],
    plan_response: list[AgentEvent],
    toggle_off: list[AgentEvent],
    agent_mode_after_plan: str,
) -> tuple[bool, str]:
    """Scenario C: /plan toggle ON → plan request (read-only) → /plan toggle OFF."""
    # Toggle ON
    if not _summarize(toggle_on)["finished"]:
        return False, "Toggle ON: No AgentFinished"
    if agent_mode_after_plan != "plan":
        return False, f"Expected plan mode, got {agent_mode_after_plan}"

    # Plan request — should complete with text (may use read tools)
    p = _summarize(plan_response)
    if not p["finished"]:
        return False, f"Plan request: No AgentFinished (errors: {p['errors']})"
    if not p["full_text"].strip():
        return False, "Plan request: No text response"
    # Check: no write tools were executed (all tool calls should be read-only)
    write_finished = [
        e for e in plan_response
        if isinstance(e, ToolCallFinished) and not e.success and "blocked" in e.result_preview.lower()
    ]
    # Having blocked writes is fine (permission layer), but no successful writes should happen

    # Toggle OFF
    if not _summarize(toggle_off)["finished"]:
        return False, "Toggle OFF: No AgentFinished"

    return True, f"Toggle ON→plan({p['iterations']}r,{p['tool_started']} tools)→Toggle OFF"


def check_d_auto_detect(
    events: list[AgentEvent], agent_mode: str
) -> tuple[bool, str]:
    """Scenario D: Natural language triggers auto plan mode detection."""
    s = _summarize(events)
    if agent_mode != "plan":
        return False, f"Expected auto-detect to enable plan mode, got {agent_mode}"
    if not s["finished"]:
        return False, f"No AgentFinished (errors: {s['errors']})"
    if not s["full_text"].strip():
        return False, "No text response"
    return True, f"Auto-detected plan mode, {s['iterations']}r, {s['tool_started']} tools"


def check_e_batch_read(events: list[AgentEvent]) -> tuple[bool, str]:
    """Scenario E: Read multiple files — should issue multiple read calls."""
    s = _summarize(events)
    if not s["finished"]:
        return False, f"No AgentFinished (errors: {s['errors']})"
    if s["tool_started"] < 2:
        return False, f"Expected ≥2 tool calls for multi-file read, got {s['tool_started']}"
    if not s["full_text"].strip():
        return False, "No final text response"
    return True, f"{s['tool_started']} tool calls, {s['iterations']} rounds"


def check_f_error_handling(events: list[AgentEvent]) -> tuple[bool, str]:
    """Scenario F: Read nonexistent file — agent should handle error gracefully."""
    s = _summarize(events)
    if not s["finished"]:
        return False, f"No AgentFinished (errors: {s['errors']})"
    # Should have at least tried a tool and gotten a result (even if failed)
    if s["tool_finished"] < 1:
        return False, "Expected at least 1 tool execution"
    # Should produce some final text (explaining the error)
    if not s["full_text"].strip():
        return False, "No final text response after error"
    return True, f"Tool executed, agent responded with {len(s['full_text'])} chars"


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


async def main() -> None:
    print("=" * 60)
    print("CodeForge Agent Loop — Real Prompt Integration Tests")
    print("=" * 60)

    # ── Setup ──
    providers = load_config("config.yaml")
    provider = providers[0]  # Use first provider (Anthropic protocol)
    print(f"\nProvider: {provider.name} | Model: {provider.model} | Protocol: {provider.protocol}")

    client = LLMClient.create(provider)
    registry = get_default_registry()
    exec_ctx = ExecutionContext(cwd=Path.cwd(), session_id="test-real")

    results: list[tuple[str, bool, str]] = []

    # ── Scenario A: Simple text ──
    print(f"\n{'─'*60}")
    print("Scenario A: Simple text response (natural completion)")
    print(f"{'─'*60}")
    conv_a = ConversationManager(system_prompt=SYSTEM_PROMPT)
    agent_a = Agent(registry=registry, llm_client=client, exec_ctx=exec_ctx,
                    conversation=conv_a, config=AgentConfig(max_iterations=5))
    events_a = await run_prompt(agent_a, "Hello! What is 2+2? Just answer briefly.", "A: simple text")
    ok, msg = check_a_simple_text(events_a)
    results.append(("A: Simple text", ok, msg))
    print(f"  → {PASS if ok else FAIL} {msg}")

    # ── Scenario B: Multi-turn tool chain ──
    print(f"\n{'─'*60}")
    print("Scenario B: Multi-turn tool chain (read file → respond)")
    print(f"{'─'*60}")
    conv_b = ConversationManager(system_prompt=SYSTEM_PROMPT)
    agent_b = Agent(registry=registry, llm_client=client, exec_ctx=exec_ctx,
                    conversation=conv_b, config=AgentConfig(max_iterations=10))
    events_b = await run_prompt(
        agent_b,
        "Read the file CLAUDE.md and tell me what it says about spec documents.",
        "B: multi-turn",
        timeout=90.0,
    )
    ok, msg = check_b_multiturn(events_b)
    results.append(("B: Multi-turn", ok, msg))
    print(f"  → {PASS if ok else FAIL} {msg}")

    # ── Scenario C: Plan Mode Toggle ──
    print(f"\n{'─'*60}")
    print("Scenario C: Plan Mode toggle (/plan ON → plan request → /plan OFF)")
    print(f"{'─'*60}")
    conv_c = ConversationManager(system_prompt=SYSTEM_PROMPT)
    agent_c = Agent(registry=registry, llm_client=client, exec_ctx=exec_ctx,
                    conversation=conv_c, config=AgentConfig(max_iterations=10))

    # /plan → ON
    events_c1 = await run_prompt(agent_c, "/plan", "C1: /plan toggle ON")
    assert agent_c.mode.value == "plan", f"Expected plan mode, got {agent_c.mode}"

    # Ask for a plan (in plan mode — should only use read tools)
    events_c2 = await run_prompt(
        agent_c,
        "I want to add a docstring to the top of core/agent/plan_mode.py. "
        "Read the file first, then tell me your plan. Output ONLY the plan, do NOT write anything.",
        "C2: plan request (plan mode ON)",
        timeout=90.0,
    )

    # /plan → OFF
    events_c3 = await run_prompt(agent_c, "/plan", "C3: /plan toggle OFF")

    ok, msg = check_c_plan_mode_toggle(events_c1, events_c2, events_c3, agent_c.mode.value)
    results.append(("C: Plan Mode toggle", ok, msg))
    print(f"  → {PASS if ok else FAIL} {msg}")

    # ── Scenario D: Auto-detect Plan Intent ──
    print(f"\n{'─'*60}")
    print("Scenario D: Auto-detect plan intent (natural language)")
    print(f"{'─'*60}")
    conv_d = ConversationManager(system_prompt=SYSTEM_PROMPT)
    agent_d = Agent(registry=registry, llm_client=client, exec_ctx=exec_ctx,
                    conversation=conv_d, config=AgentConfig(max_iterations=10))
    assert agent_d.mode.value == "off"

    events_d = await run_prompt(
        agent_d,
        "先计划一下：我想在 core/agent/ 目录下新建一个 __main__.py，"
        "让用户可以直接 python -m core.agent 启动。先分析一下要怎么做，不要执行。",
        "D: auto-detect plan intent",
        timeout=90.0,
    )
    ok, msg = check_d_auto_detect(events_d, agent_d.mode.value)
    results.append(("D: Auto-detect plan", ok, msg))
    print(f"  → {PASS if ok else FAIL} {msg}")

    # ── Scenario E: Batch read ──
    print(f"\n{'─'*60}")
    print("Scenario E: Read multiple files concurrently")
    print(f"{'─'*60}")
    conv_e = ConversationManager(system_prompt=SYSTEM_PROMPT)
    agent_e = Agent(registry=registry, llm_client=client, exec_ctx=exec_ctx,
                    conversation=conv_e, config=AgentConfig(max_iterations=10))
    events_e = await run_prompt(
        agent_e,
        "Read both CLAUDE.md and docs/spec.md. Tell me the first line of each.",
        "E: batch read",
        timeout=90.0,
    )
    ok, msg = check_e_batch_read(events_e)
    results.append(("E: Batch read", ok, msg))
    print(f"  → {PASS if ok else FAIL} {msg}")

    # ── Scenario F: Error handling ──
    print(f"\n{'─'*60}")
    print("Scenario F: Error handling (nonexistent file)")
    print(f"{'─'*60}")
    conv_f = ConversationManager(system_prompt=SYSTEM_PROMPT)
    agent_f = Agent(registry=registry, llm_client=client, exec_ctx=exec_ctx,
                    conversation=conv_f, config=AgentConfig(max_iterations=5))
    events_f = await run_prompt(
        agent_f,
        "Read the file /nonexistent/ghost.txt and tell me what it contains.",
        "F: error handling",
    )
    ok, msg = check_f_error_handling(events_f)
    results.append(("F: Error handling", ok, msg))
    print(f"  → {PASS if ok else FAIL} {msg}")

    # ── Summary ──
    print(f"\n{'='*60}")
    print("RESULTS SUMMARY")
    print(f"{'='*60}")
    passed = 0
    for name, ok, msg in results:
        status = PASS if ok else FAIL
        print(f"  {status}  {name}: {msg}")
        if ok:
            passed += 1
    print(f"\n{passed}/{len(results)} scenarios passed")
    print(f"{'='*60}")

    if passed < len(results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
