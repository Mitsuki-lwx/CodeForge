"""长对话 prompt cache 实测：命中率随对话轮次如何变化。

单条重复请求是最优场景（稳定前缀几乎全额命中），但真实 ReAct 会话每轮都要
重发「系统提示 + 全部历史消息」，逐轮累积。本脚本模拟这种多轮累积，逐轮测量
「未命中(input) / 命中(cache_read) / 总输入 / 命中率」，看缓存价值随对话变长
如何衰减，以及瓶颈在哪。

跑法：
  python scripts/measure_cache_long.py                 # anthropic 协议，15 轮
  python scripts/measure_cache_long.py --turns 20      # 指定轮数
  python scripts/measure_cache_long.py --provider OpenAI
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.loader import load_config
from config.model import ProviderConfig
from conversation.message import APIMessage
from core.prompts.builder import PromptBuilder
from llm.client import LLMClient
from llm.stream_events import CompletionDone, StreamError


# ── 代表性工具定义（仅作稳定前缀）────────────────────────────────
_TOOL_DEFS = [
    {
        "name": t,
        "description": f"Tool {t}.",
        "input_schema": {
            "type": "object",
            "properties": {"arg": {"type": "string"}},
            "required": ["arg"],
        },
    }
    for t in ("bash", "read_file", "write_file", "edit_file", "glob", "grep", "agent", "state")
]

_MEMORY_INDEX = (
    "长期记忆索引：\n"
    "- project-name: 项目名为 CodeForge\n"
    "- prompt-cache-wiring: 真 prompt cache 已接通\n"
    "- team-tool-visibility: spawn 时重建专用 registry\n"
)

_INSTRUCTIONS = "遵循项目规范；改动匹配周边风格；不可逆操作先确认。"

_ENV_INFO = "工作目录: D:\\python\\CodeForge\n平台: win32\n日期: 2026-08-17\n"


# ── 脚本化的真实对话（用户提问 + 助手回答，逐轮累积）──────────────
# 每轮内容固定，保证跨轮可精确对比缓存命中；长度接近真实编码会话。
_USER_TURNS = [
    "帮我重构 core/agent/agent.py 里的 run 方法，把职责拆开。",
    "先看一下 run 方法现在做了哪些事，列出它的职责。",
    "把状态切换的部分抽成一个独立方法。",
    "抽出来后，循环驱动部分怎么处理？",
    "注意 pre_step 钩子要在每轮 LLM 调用前触发。",
    "异常处理呢？trace 关闭要放在 finally 里。",
    "再检查一下 switch_model 会不会影响正在跑的循环。",
    "给这个重构写个单元测试。",
    "测试里要覆盖 idle/running 状态切换。",
    "还有 max-tokens 粘性续跑的场景。",
    "把改动整理成 commit。",
    "commit message 要符合项目风格。",
    "顺带更新一下 README 的架构说明。",
    "最后跑一遍全量测试确认没回归。",
    "总结这次改动，写进会话总结。",
]

_ASSISTANT_ANSWER = None  # 弃用：改为每轮唯一回答（见 _answer）


def _answer(i: int) -> str:
    """生成第 i 轮的唯一助手回答（含轮次标记，长度接近真实编码会话）。

    每轮内容不同，消除「同一字符串重复」对缓存测量的影响；缓存结论只取决于
    「历史是否逐字重发」这一点，与回答内容是否相同无关。
    """
    return (
        f"第 {i + 1} 轮的改动我已完成。本轮把 `_drive_loop` 里的事件分发再拆出 "
        f"`_emit_{i}` 子方法，并把状态切换集中到边界处理。\n\n"
        f"```python\n"
        f"async def _emit_{i}(self, ev):\n"
        f"    if isinstance(ev, ToolUse):\n"
        f"        await self._handle_tool(ev)  # 本轮新增分支 {i}\n"
        f"    elif isinstance(ev, TextChunk):\n"
        f"        self._buffer.append(ev.text)  # 缓冲 {i}\n"
        f"    yield ev\n"
        f"```\n\n"
        f"这一轮的单元测试新增了 {i + 1} 个断言，覆盖事件分发与状态切换。"
        f"测试已通过，未发现回归。"
    )


def _utf8_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


async def _one_round(client: LLMClient, assembly, messages: list[APIMessage]) -> dict:
    usage: dict = {}
    cache_read = 0
    async for ev in client.stream_chat(messages, system_blocks=assembly):
        if isinstance(ev, StreamError):
            raise RuntimeError(f"LLM 错误: {ev.code} {ev.message}")
        if isinstance(ev, CompletionDone):
            if ev.usage:
                usage = ev.usage
            cache_read = ev.cache_read_input_tokens
    return {
        "input": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "cache_read": int(cache_read),
    }


def _metrics(protocol: str, r: dict) -> tuple[int, int]:
    """返回 (总输入, 命中)。"""
    if protocol == "openai":
        return r["input"], r["cache_read"]  # prompt_tokens 已含命中
    return r["input"] + r["cache_read"], r["cache_read"]  # anthropic: input 为未命中


async def _run(provider: ProviderConfig, turns: int) -> None:
    client = LLMClient.create(provider)
    builder = PromptBuilder(model=provider.model, instructions=_INSTRUCTIONS, memory=_MEMORY_INDEX)
    assembly = builder.build_full(env_info=_ENV_INFO, tool_defs=_TOOL_DEFS)

    stable_chars = sum(len(b.content) for b in assembly.cached)
    print(f"\n== 长对话缓存实测 {provider.name} ({provider.protocol}, {provider.model}) ==")
    print(f"   稳定前缀约 {stable_chars} 字符；模拟 {turns} 轮累积对话（每轮重发全部历史）\n")

    history: list[APIMessage] = []
    rows: list[tuple[int, int, int, float]] = []
    print(f"{'轮':>3} {'总输入':>7} {'命中':>7} {'未命中':>7} {'命中率':>8}")
    print("-" * 40)

    for i in range(turns):
        user = _USER_TURNS[i % len(_USER_TURNS)]
        if history:
            history.append(APIMessage(role="assistant", content=_answer(i - 1)))
        history.append(APIMessage(role="user", content=user))

        r = await _one_round(client, assembly, history)
        total, hit = _metrics(provider.protocol, r)
        miss = total - hit
        rate = hit / total if total else 0.0
        rows.append((total, hit, miss, rate))
        print(f"{i + 1:>3} {total:>7} {hit:>7} {miss:>7} {rate:>7.1%}")

    # 汇总（跳过冷启动第 1 轮）
    warm = rows[1:]
    med_rate = sorted(x[3] for x in warm)[len(warm) // 2]
    avg_miss = sum(x[2] for x in warm) / len(warm)
    last_total, last_hit, _, last_rate = rows[-1]
    print("\n   ── 结果（跳过冷启动第 1 轮）──")
    print(f"   命中率：中位数 {med_rate:.1%}，末轮 {last_rate:.1%}（{last_hit}/{last_total} tokens）")
    print(f"   每轮平均仅新增 {avg_miss:.0f} tokens 重新计费，其余（含全部历史）命中缓存")
    print(
        f"   可写进简历的一句话：「{turns} 轮长对话中，prompt cache 命中率稳定在 "
        f"{med_rate:.0%} 上下（中位数），每轮仅增量部分重新计费」"
    )


async def _main() -> None:
    ap = argparse.ArgumentParser(description="长对话 prompt cache 实测")
    ap.add_argument("--provider", default="", help="按 name 选 provider（默认第一个）")
    ap.add_argument("--turns", type=int, default=15, help="对话轮数")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    providers = load_config(args.config)
    provider = providers[0]
    if args.provider:
        for p in providers:
            if p.name == args.provider:
                provider = p
                break
        else:
            print(f"未找到 provider '{args.provider}'")
            sys.exit(1)

    await _run(provider, max(2, args.turns))


if __name__ == "__main__":
    _utf8_stdout()
    asyncio.run(_main())
