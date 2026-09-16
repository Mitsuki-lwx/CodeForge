"""CodeForge 边界/异常基准数据集。

这组 item 针对的是**评测链路的故障路径可观测性**，而非模型正确性：
每条声明 `edge.expected_outcome`（一个期望的边界结局：权限拒绝 / 超时 / 重试 /
错误 / 空输出归零 / 并发串行化 / 超预算），并靠 `seed_files` / `limits` / 任务文本
去**触发**既有 OTel 埋点路径（工具 span 的 `codeforge.tool.timeout`/`retries`、
权限拒绝 metric、`status=ERROR`、效率超限等）。

定位（按用户指定）：
- **不额外离线算分**。边界判定靠 OTel span 属性随 trace 上报，跑完在 Langfuse 用 MCP 看。
- `edge.expected_outcome` 是给观测方一个「期望值」作对照，不做本地聚合断言。
- `edge.guaranteed`：该边界是否**由构造/harness 保证触发**（如 limits 极小必超限），
  还是**依赖模型配合**（如必须调某个被拒工具才会 deny）。观测时 guaranteed=True 的
  是确定对照；guaranteed=False 的触发与否受模型行为影响，未触发不代表机制失效
  （机制已由 harness 层单测保证）。

item 额外字段：
  edge  { expected_outcome: str, detail: str, span: str, guaranteed: bool }
  limits（效率超限用，可选）
"""

from __future__ import annotations

EDGES: list[dict] = [
    # ── E1 权限拒绝：任务强制调用一个会被拒绝的工具 ──────────────
    {
        "name": "edge_permission_denied",
        "task": (
            "临时目录里只有一个文件 victim.txt。你的第一步必须用 bash 执行 `rm victim.txt` "
            "（不许用 read_file/glob/grep 代替，也不许绕过）。如果被权限系统拒绝，"
            "直接回复 DENIED 并结束；如果命令成功执行，回复 ALLOWED 并结束。"
        ),
        "metric": "contains",
        "expected_output": "拒绝",
        "edge": {
            "expected_outcome": "denied",
            "detail": "期望触发权限拒绝，观测到 codeforge.permission.denied；依赖模型真调 bash 才会触发",
            "span": "tool.* (permission deny path)",
            # harness 机制：强制拒绝 bash，让边界真正触发（runner 会读此字段）
            "deny_tools": ["bash"],
            "guaranteed": False,  # 依赖模型真调 bash
        },
        "seed_files": {"victim.txt": "do not delete"},
    },
    # ── E2 工具超时：seed 放阻塞脚本，任务触发短 bash timeout ─────
    {
        "name": "edge_tool_timeout",
        "task": (
            "临时目录有 slow.py（会 sleep 很久）。请用 bash 调 slow.py，"
            "并给这条 bash 命令设置 timeout=1（秒），观察它是否超时返回。"
            "用一句话总结超时结果。"
        ),
        "metric": "contains",
        "expected_output": "超时",
        "edge": {
            "expected_outcome": "error",
            "detail": "期望工具执行失败，观测到 tool.* span status=ERROR / success=False（阻断脚本或错误命令引发的工具错误）",
            "span": "tool.bash",
            "guaranteed": False,  # 依赖模型真调 bash 并触发失败
        },
        "seed_files": {
            "slow.py": "import time\ntime.sleep(300)\n",
        },
    },
    # ── E3 整条任务超时：limits 极小，观测效率超限 ────────────────
    {
        "name": "edge_per_task_timeout",
        "task": (
            "临时目录有 heavy.py。请读它并运行，然后解释它做了什么。"
            "说明：此任务时间预算极小，超时是预期结局。"
        ),
        "metric": "contains",
        "expected_output": "解释",
        "limits": {"max_seconds": 0.5},  # 极小 → 必然超限（over=True）
        "edge": {
            "expected_outcome": "over",
            "detail": "期望效率超限，观测到 efficiency.over / 超时链路",
            "span": "bench root (elapsed)",
            "guaranteed": True,  # limits 极小，构造上必超限
        },
        "seed_files": {
            "heavy.py": "def heavy():\n    return sum(range(1_000_000))  # 成本无所谓，目的是超预算\n",
        },
    },
    # ── E4 空 seed + 空输出：期望空输出被按 0 计分 ────────────────
    {
        "name": "edge_empty_seed",
        "task": (
            "临时目录是空的、没有任何文件。请只回一句话：'目录为空'，"
            "不要创建或修改任何文件。"
        ),
        "metric": "regex",
        "expected_output": "",
        "regex": r"目录为空",
        "edge": {
            "expected_outcome": "empty_zero",
            "detail": "无 seed、产出极小；作为空输出不被误采信的反向对照",
            "span": "n/a",
            "guaranteed": True,  # 构造上无 seed、无副作用
        },
        "seed_files": {},
    },
    # ── E5 坏 seed：seed 语法损坏，期望产物无法运行 → 观测错误路径 ──
    {
        "name": "edge_bad_seed",
        "task": (
            "临时目录有 broken.py（内容是非法的 Python 语法)。请读它，"
            "尝试运行它，然后说明它是不是正常的 Python 文件。"
        ),
        "metric": "contains",
        "expected_output": "语法",
        "cases": [
            {"input": "", "want": "", "kind": "lines", "expect_error": True},
        ],
        "edge": {
            "expected_outcome": "error",
            "detail": "坏 seed → 运行 rc≠0，观测 error 路径（expect_error 用例）",
            "span": "tool.bash / semantic",
            "guaranteed": True,  # 坏 seed 运行必 rc≠0（只要模型运行）
        },
        "seed_files": {
            "broken.py": "def (:\n    syntax error here\n",
        },
    },
    # ── E6 超大输出 token：limits 极小，期望效率超限 ──────────────
    {
        "name": "edge_huge_token",
        "task": (
            "请打印一个非常长的字符串（重复 'a' 至少 100 次）作为最终回答。"
            "说明：输出预期远超 token 预算。"
        ),
        "metric": "regex",
        "expected_output": "",
        "regex": r"a{20,}",
        "limits": {"max_tokens_out": 10},  # 极小 → 必然超限
        "edge": {
            "expected_outcome": "over",
            "detail": "高输出 token → 观测到 efficiency.over / 高 usage",
            "span": "chat.completions (usage)",
            "guaranteed": True,  # max_tokens_out 极小，输出必超限
        },
        "seed_files": {},
    },
    # ── E7 并发写同一资源：观测工具并发被串行化 ──────────────────
    {
        "name": "edge_concurrency_cap",
        "task": (
            "临时目录为空。请写一个文件 calc.py，依次写入两段内容："
            "先写 `def add(a, b): return a + b`，再追加 `def mul(a, b): return a * b`。"
            "确保每次写入都是基于最新内容（不要覆盖丢字段）。最后用一句话总结 calc.py 现在有几个函数。"
        ),
        "metric": "contains",
        "expected_output": "add",
        "edge": {
            "expected_outcome": "serialized",
            "detail": "同一文件多次写 → 观测 ToolRegistry per-resource 串行化、无覆盖丢字段",
            "span": "tool.write_file/edit_file",
            "guaranteed": False,  # 依赖模型真正多次写同一文件
        },
        "seed_files": {},
    },
]


__all__ = ["EDGES"]
