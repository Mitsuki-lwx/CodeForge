"""测量 LLM prompt cache 命中率（协议感知版）。

用途：给简历/技术报告提供可复现的量化结果（不瞎编）。

跑法：
  python scripts/measure_cache.py                      # 默认第一个 provider
  python scripts/measure_cache.py --provider OpenAI    # 按 name 选 provider
  python scripts/measure_cache.py --rounds 3           # 连发轮数（默认 2）

做法：
  1. 用真实系统提示模块 + 代表性工具定义 + 记忆/指令，拼出与 agent 实际请求
     同构的稳定前缀（PromptAssembly.cached 块），环境信息放 uncached 尾块。
  2. 对完全相同的请求连发 N 轮，读每轮 CompletionDone 里的用量字段。
  3. 按协议语义正确计算命中率。

协议语义（务必区分，这是关键）：
  - Anthropic 端点：`input_tokens` = 未命中量，`cache_read_input_tokens` = 命中量。
      总输入 = input + cache_read；命中率 = cache_read / (input + cache_read)。
  - OpenAI  端点：`prompt_tokens` = 总量（已含命中），`prompt_cache_hit_tokens` /
      `prompt_tokens_details.cached_tokens` = 命中量。
      总输入 = prompt_tokens；命中率 = hit / prompt_tokens。

说明：
  - 命中率衡量「重复请求的稳定前缀里有多少 token 命中缓存」，是缓存效率指标，
    不是「整段会话成本下降多少」——真实会话中只有稳定前缀（系统提示+工具定义）
    会命中，逐轮累积的对话历史不会。缓存命中 token 也不是免费（按折扣计费）。
  - 工具定义为代表性 schema（仅作稳定前缀，不影响缓存结论——结论只取决于上游
    真实返回的用量字段）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本能从任意目录独立运行（把项目根目录加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import load_config
from config.model import ProviderConfig
from conversation.message import APIMessage
from core.prompts.builder import PromptBuilder
from llm.client import LLMClient
from llm.stream_events import CompletionDone, StreamError


# ── 代表性工具定义（仅作稳定前缀，内容不影响缓存结论）────────────────
_TOOL_DEFS = [
    {
        "name": "bash",
        "description": "Run a shell command in the project working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."},
                "timeout": {"type": "integer", "description": "Timeout in ms."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path."},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file, overwriting if present.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Perform an exact string replacement in a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "glob",
        "description": "Match files by glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Search file contents with a regex.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "agent",
        "description": "Delegate a subtask to a sub-agent.",
        "input_schema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "state",
        "description": "Read or update session state (goal/todo/constraint).",
        "input_schema": {
            "type": "object",
            "properties": {"op": {"type": "string"}},
            "required": ["op"],
        },
    },
]

_MEMORY_INDEX = (
    "长期记忆索引：\n"
    "- project-name: 项目名为 CodeForge\n"
    "- prompt-cache-wiring: 真 prompt cache 已接通（cache_control 断点）\n"
    "- team-tool-visibility: spawn 时重建专用 registry\n"
    "- openai-wire-format-gotchas: LLM 层 transport/adapters/session 拆分\n"
)

_INSTRUCTIONS = (
    "遵循项目根目录 CLAUDE.md 的规范；改动代码时匹配周边风格；"
    "对不可逆或对外操作先确认。"
)

_ENV_INFO = "工作目录: D:\\python\\CodeForge\n平台: win32\n日期: 2026-08-17\n"


def _utf8_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


async def _one_round(client: LLMClient, assembly, messages: list[APIMessage]) -> dict:
    """发一次请求，返回 {input, output, cache_read, cache_creation}。

    `input` 为协议原始「输入」字段（anthropic=input_tokens 未命中量；openai=prompt_tokens 总量），
    `cache_read` 为命中量。二者语义由调用方按 protocol 区分。
    """
    usage: dict = {}
    cache_read = cache_creation = 0
    async for ev in client.stream_chat(messages, system_blocks=assembly):
        if isinstance(ev, StreamError):
            raise RuntimeError(f"LLM 错误: {ev.code} {ev.message}")
        if isinstance(ev, CompletionDone):
            if ev.usage:
                usage = ev.usage
            cache_read = ev.cache_read_input_tokens
            cache_creation = ev.cache_creation_input_tokens
    return {
        "input": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "cache_read": int(cache_read),
        "cache_creation": int(cache_creation),
    }


def _hit(protocol: str, r: dict) -> tuple[int, int]:
    """按协议语义返回 (总输入 tokens, 命中 tokens)。"""
    if protocol == "openai":
        # prompt_tokens 已含命中量（总量）
        return r["input"], r["cache_read"]
    # anthropic：input_tokens 为未命中量，总输入 = input + cache_read
    return r["input"] + r["cache_read"], r["cache_read"]


async def _run(provider: ProviderConfig, rounds: int) -> None:
    client = LLMClient.create(provider)
    builder = PromptBuilder(model=provider.model, instructions=_INSTRUCTIONS, memory=_MEMORY_INDEX)
    assembly = builder.build_full(env_info=_ENV_INFO, tool_defs=_TOOL_DEFS)

    print(f"\n== 测量 {provider.name} ({provider.protocol}, {provider.model}) ==")
    print(f"   稳定前缀（cached 块）约 {sum(len(b.content) for b in assembly.cached)} 字符")
    print(f"   连发 {rounds} 轮完全相同的请求…\n")

    message = APIMessage(role="user", content="请列出当前项目的核心模块，并说明各自的职责。")
    results = []
    for i in range(rounds):
        r = await _one_round(client, assembly, [message])
        results.append(r)
        total, hit = _hit(provider.protocol, r)
        rate = hit / total if total else 0.0
        print(
            f"   第 {i + 1} 轮: input={r['input']:<5} output={r['output']:<4} "
            f"cache_read={r['cache_read']:<5} → 总输入 {total}, 命中 {hit} ({rate:.1%})"
        )

    total, hit = _hit(provider.protocol, results[-1])
    rate = hit / total if total else 0.0

    print("\n   ── 结果 ──")
    print(f"   稳定前缀命中率: {rate:.1%}（{hit}/{total} tokens 命中缓存）")
    print(
        "   可写进简历的一句话："
        f"「{provider.protocol} 协议下，稳定系统前缀（系统提示+工具定义）"
        f"缓存命中率 {rate:.1%}（重复请求 {total} tokens 中 {hit} 命中缓存）」"
    )
    print(
        "   注意：这是「稳定前缀」在相同请求下的命中率，不等于整段会话成本下降——"
        "真实会话中逐轮累积的对话历史不会命中缓存。"
    )


async def _main() -> None:
    ap = argparse.ArgumentParser(description="测量 prompt cache 命中率（协议感知）")
    ap.add_argument("--provider", default="", help="按 name 选 provider（默认第一个）")
    ap.add_argument("--rounds", type=int, default=2, help="连发轮数")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = ap.parse_args()

    providers = load_config(args.config)
    provider = providers[0]
    if args.provider:
        for p in providers:
            if p.name == args.provider:
                provider = p
                break
        else:
            print(f"未找到 provider '{args.provider}'，可用: {[p.name for p in providers]}")
            sys.exit(1)

    await _run(provider, max(2, args.rounds))


if __name__ == "__main__":
    _utf8_stdout()
    asyncio.run(_main())
