"""真实 agent 端到端:起一个小项目,让它干活,然后 dump audit 审计轨迹。

用法: python scripts/run_trace_demo.py
(需要 config.yaml 里配置了可用的 provider + API key)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from config.loader import load_config
from conversation.manager import ConversationManager
from core.agent import Agent, AgentConfig
from core.permissions.modes import PermissionMode
from core.tool.context import ExecutionContext
from core.tool.tools import get_default_registry
from llm.client import LLMClient


def _build_tiny_project(dirpath: Path) -> str:
    """造一个带 bug 的超小 Python 项目 + 单测。"""
    py = dirpath / "calc.py"
    py.write_text(
        "def add(a, b):\n"
        "    return a - b  # bug: should be + \n"
        "\n"
        "def mul(a, b):\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    test = dirpath / "test_calc.py"
    test.write_text(
        "from calc import add, mul\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5   # fails: returns -1\n"
        "\n"
        "def test_mul():\n"
        "    assert mul(2, 3) == 6\n",
        encoding="utf-8",
    )
    (dirpath / "__init__.py").write_text("", encoding="utf-8")
    return "calc.py"


def main() -> None:
    providers = load_config("config.yaml")
    if not providers:
        print("no provider configured")
        return
    provider = providers[0]
    print(f"[demo] provider = {provider.name} ({provider.model})")

    work = Path(tempfile.mkdtemp(prefix="codeforge_demo_"))
    audit = work / "audit_home"
    target = _build_tiny_project(work)
    print(f"[demo] tiny project at: {work}")
    print(f"[demo] audit dir:        {audit}")

    registry = get_default_registry()
    client = LLMClient.create(provider)
    exec_ctx = ExecutionContext(cwd=work, session_id="demo-session-001")
    conv = ConversationManager(
        system_prompt="You are CodeForge. Use tools to inspect and edit code. "
                      "When done, reply with a short summary."
    )
    cfg = AgentConfig(max_iterations=12, result_preview_max_chars=200)
    agent = Agent(
        registry=registry,
        llm_client=client,
        exec_ctx=exec_ctx,
        conversation=conv,
        config=cfg,
        runtime=None,
        trace_audit_dir=str(audit),
    )
    # 无头演示:跳过 HITL,权限检查直接判定(不阻塞挂起等人工确认)
    agent.set_permission_mode(PermissionMode.BYPASS)

    instruction = (
        f"项目在 {work} .文件 {target} 里的 add() 有一个 bug(应该返回和而不是差)。"
        f"请: 1) 读 calc.py 2) 修复它 3) 运行 pytest 验证 test_calc.py 通过。"
        f"做完用一句话总结改了哪个文件、测试是否通过。"
    )
    print(f"\n[demo] instruction: {instruction}\n{'='*70}")

    events = []
    asyncio.run(_collect(agent, instruction, events))

    print(f"\n{'='*70}\n[demo] agent run finished. events received by UI: {len(events)}")

    # dump audit
    pf = audit / "audit" / "demo-session-001.jsonl"
    if not pf.exists():
        print("[demo] !! no audit file written")
        return
    print(f"\n[demo] audit file: {pf}\n")
    lines = [json.loads(l) for l in pf.read_text(encoding="utf-8").splitlines() if l.strip()]
    for ev in lines:
        print("  " + json.dumps(ev, ensure_ascii=False))

    from core.trace.reader import session_summary
    print("\n[demo] summary:")
    print("  " + json.dumps(session_summary("demo-session-001", audit_dir=str(audit)), ensure_ascii=False))

    # cleanup
    shutil.rmtree(work, ignore_errors=True)


async def _collect(agent, instruction, events):
    async for ev in agent.run(instruction):
        events.append(ev)


if __name__ == "__main__":
    main()
