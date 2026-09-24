"""CodeForge — 多协议 LLM 终端对话客户端入口。"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CLI 参数；`--team-member` 出现时走 pane 队友自治循环分支。

    子命令（host / runs / attach）用**可选位置参数**分发，而不是 argparse 的
    `subparsers`：既有的 `--team-member` 调用方（`Team` spawn 起的 pane 队友进程）
    传的是一串平铺 flag，改成 subparsers 会把这些 flag 全挤到某个子命令下面，
    破坏现有调用。位置参数与 flag 不冲突——argparse 会把值优先给紧邻的 option，
    所以 `--member host` 不会把 `host` 误当子命令。
    """
    p = argparse.ArgumentParser(prog="codeforge", description="终端 AI 编程助手")

    # 合法档位取自 `UnattendedPolicy`（**单一事实来源**），不在这里手抄一份字符串
    # 列表 —— 手抄的后果是"新增了档位但 CLI 选不了"：`review` 加进枚举后，
    # 这里的硬编码列表没跟着更新，`--unattended-policy review` 被 argparse 直接
    # 拒掉，新档位在命令行上等于不存在。同类问题在配置解析里已经踩过一次。
    from core.permissions.modes import UnattendedPolicy

    p.add_argument(
        "command",
        nargs="?",
        default="",
        help="host（前台起会话宿主）/ runs（列 run）/ attach（查看或继续某个 run）；省略则进 TUI",
    )
    p.add_argument("target", nargs="?", default="", help="attach 的 run_id")
    p.add_argument(
        "--continue",
        dest="resume",
        action="store_true",
        help="attach 子命令：继续该 run（不自动重跑此前已执行过的副作用）",
    )
    p.add_argument(
        "--prompt",
        default="",
        help="attach --continue 的续跑输入（省略时发「继续」）",
    )
    p.add_argument(
        "--team-member",
        action="store_true",
        help="以 pane 队友自治循环启动（由 Team spawn 调用）",
    )
    p.add_argument("--team", default="")
    p.add_argument("--member", default="")
    p.add_argument("--agent-id", default="")
    p.add_argument("--session-dir", default="")
    p.add_argument("--worktree", default="")
    p.add_argument("--agent-type", default="")
    p.add_argument("--model", default="")
    p.add_argument("--plan-mode", action="store_true")
    p.add_argument(
        "--task", default="", help="Run a single task headlessly (skip TUI), then exit"
    )
    p.add_argument(
        "--loop", default="", help="Agent 循环策略：react 或自定义模块路径（spec_loop）"
    )
    p.add_argument(
        "--unattended-policy",
        dest="unattended_policy",
        default=None,  # None = 未指定；交给 config 的 features.host.unattended_policy
        choices=[pol.value for pol in UnattendedPolicy],
        help=(
            "host 子命令：无人值守下如何代答 ask 级工具决策。"
            "省略时取 config 的 features.host.unattended_policy（默认 deny_all）。"
            "deny_all 一律拒绝（最保守）；allow_write 另放行写文件（需要 agent 改代码时用）；"
            "allow_all 全放；review 由独立审查者逐个判断（见 spec_approval_review）。"
            "只读工具本就不询问、不受本策略影响；"
            "交互式工具（计划确认等）任何档位都拒。"
            "⚠️ review 档需要配好审批审查后端（features.approval_review），"
            "拿不到审查者时会**降级为 deny_all**。"
        ),
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="host 子命令：监听端口（0 = 随机）。省略时取 features.host.port。",
    )
    p.add_argument(
        "--host",
        dest="host",
        action="store_true",
        default=None,
        help=(
            "TUI 以客户端模式启动：连本工作区正在跑的 host（连不上则在本进程内嵌一个）。"
            "省略时取 features.host.enabled（默认关）。对 --task 无效。"
        ),
    )
    p.add_argument(
        "--no-host",
        dest="host",
        action="store_false",
        help="强制关掉 host 模式，即使 features.host.enabled 为 true。",
    )
    return p.parse_args(argv)


# ── 会话宿主 CLI（任务 12）────────────────────────────────────────
#
# 三个命令的分工刻意不对称：
#   * `runs`  **不经过 host** —— 没有 host 在跑的时候恰恰最需要看历史，而那时客户端
#     根本连不上；而且不先做 stale 改判的话，列表会把一个已经死掉的进程显示成 running。
#   * `attach` 先离线读 run 记录给出状态（host 不在也能看到），再在 host 在跑时补上
#     对话摘要。这样"host 没起"只影响摘要，不影响状态可见性。
#   * `host`  是唯一需要装配整个 agent 栈的命令，因此这里**延迟 import**
#     `core.host.server`（它依赖 core.agent.bootstrap）。


def _fmt_ts(ts: float) -> str:
    """把 unix 秒转成本地时间字符串（列表/详情共用一种口径）。"""
    import time

    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (TypeError, ValueError, OSError):
        return "-"


def _primary_provider():
    """取 config.yaml 里第一个 provider（host 是无人值守入口，不做交互式选择）。"""
    from config.loader import load_config

    providers = load_config("config.yaml")
    if not providers:
        raise SystemExit("config.yaml 里没有可用的 provider，无法启动 host")
    return providers[0]


def _cmd_runs() -> int:
    """`codeforge runs` —— 列出 run（run_id / session / status / 时间）。"""
    from core.host import RunLifecycle, RunStore

    workspace = Path.cwd()
    store = RunStore(workspace)
    # 先改判僵尸 run，再列表：反过来的话死进程会显示成 running
    stale = RunLifecycle(store).mark_stale_interrupted()
    runs = store.list()
    if not runs:
        print("该工作区还没有 run 记录。先 `codeforge host` 起一个会话。")
        return 0

    print(f"{'RUN_ID':<34} {'SESSION':<26} {'STATUS':<12} 时间")
    for r in runs:
        print(f"{r.id:<34} {r.session_id:<26} {r.status.value:<12} {_fmt_ts(r.updated_at)}")
    if stale:
        print(f"\n（启动时把 {len(stale)} 个僵尸 run 改判为 interrupted）")
    print(f"\n共 {len(runs)} 个 run。查看详情：codeforge attach <run_id>")
    return 0


def _read_journal_tail(session_dir: Path, limit: int = 5) -> list:
    """读该会话的副作用 journal 尾部（只读；读不到就返回空，不打断 attach）。"""
    from core.host import read_journal

    try:
        entries = read_journal(session_dir)
    except Exception:  # noqa: BLE001 —— journal 读失败不该让 attach 失败
        return []
    return entries[-limit:]


@dataclass
class _PendingEffect:
    """attach 要展示的一条待确认副作用：journal 的判定 + 调用参数。

    `summary` 来自 journal，存的是**工具返回内容**——写类工具成功时常常是空串
    （实测 `write_file` 就是，它的返回内容为空），只报它会得到
    「- write_file: 」这种不含任何信息的告警。真正有用的是**调用参数**
    （写的是哪个文件、跑的是什么命令），所以从会话里一并带出来。
    """

    tool_use_id: str
    tool_name: str
    summary: str
    tool_input: dict


# 参数里最能说明"影响面"的键，按优先级取第一个有值的
_INPUT_HINT_KEYS = ("file_path", "path", "command", "pattern", "url", "query")


def _input_hint(tool_input: dict) -> str:
    """从调用参数里挑一句「改了哪里」；挑不到返回空串（调用方据此省略冒号）。"""
    for key in _INPUT_HINT_KEYS:
        value = tool_input.get(key)
        if value:
            return str(value)
    return ""


def _pending_confirmations(session_dir: Path) -> list[_PendingEffect]:
    """列出「可能已经生效、但结果没落盘」的副作用调用（spec 的恢复语义）。

    判据是**交叉**两个来源：会话里未配对的 `tool_use` × journal 里同 id 的记录。
    只有两边都命中才报警——单看 journal 会把正常完成的调用也列出来（假警报会让人
    很快学会忽略真警报），单看未配对项则分不清"工具还没跑"和"跑完了但结果没落盘"。

    调用参数取自同一批未配对项（`Message.tool_input`），不额外读一遍会话。

    读不到就返回空：attach 的主职责是查看状态，不该被恢复判定拖垮。
    """
    from core.archive import read_unpaired_tool_uses
    from core.host import find_pending_confirmations

    try:
        unpaired = read_unpaired_tool_uses(session_dir)
        inputs = {m.tool_use_id: dict(m.tool_input or {}) for m in unpaired}
        return [
            _PendingEffect(
                tool_use_id=p.tool_use_id,
                tool_name=p.tool_name,
                summary=p.summary,
                tool_input=inputs.get(p.tool_use_id, {}),
            )
            for p in find_pending_confirmations(session_dir, unpaired)
        ]
    except Exception:  # noqa: BLE001 —— 恢复判定失败不该让 attach 失败
        return []


def _print_run_detail(record, workspace: Path) -> None:
    """离线部分：状态信息（不需要 host 就能给）。"""
    print(f"run_id   : {record.id}")
    print(f"session  : {record.session_id}")
    print(f"status   : {record.status.value}")
    print(f"workspace: {record.workspace}")
    print(f"创建     : {_fmt_ts(record.created_at)}")
    print(f"更新     : {_fmt_ts(record.updated_at)}")
    if record.pid:
        print(f"pid      : {record.pid}")
    if record.exit_reason:
        print(f"结束原因 : {record.exit_reason}")


def _print_conversation(payload: dict, limit: int = 8) -> None:
    """打印对话摘要（尾部若干条）。字段缺失时降级显示，不抛栈。"""
    messages = payload.get("messages") or []
    total = payload.get("total")
    print(f"\n对话摘要（共 {total if total is not None else len(messages)} 条，显示末尾 {len(messages)} 条）：")
    if not messages:
        print("  （空）")
        return
    for m in messages[-limit:]:
        if not isinstance(m, dict):
            print(f"  - {str(m)[:120]}")
            continue
        role = m.get("role") or m.get("type") or "?"
        text = m.get("text") or m.get("content") or m.get("summary") or ""
        text = " ".join(str(text).split())
        print(f"  [{role}] {text[:160]}")


async def _attach_async(workspace: Path, record, args: argparse.Namespace) -> int:
    """`attach` 的在线部分：对话摘要 / 继续该 run。"""
    from core.host import HostClient, HostError, HostNotRunningError

    try:
        client = await HostClient.connect(workspace)
    except HostNotRunningError as e:
        # 消息里已带"先 codeforge host"的指引（HostNotRunningError 统一了三种连不上）
        print(f"\nhost 未在运行：{e}")
        return 1
    except HostError as e:
        print(f"\n连上了 host 但握手被拒：{e}")
        return 1

    try:
        if args.resume:
            return await _continue_run(client, record, args, workspace)

        try:
            payload = await client.read_conversation(record.id, limit=8)
        except Exception as e:  # noqa: BLE001 —— 摘要拿不到不影响状态展示
            print(f"\n（读对话摘要失败：{e}）")
            return 0
        _print_conversation(payload)
        print(f"\n继续这个 run：codeforge attach {record.id} --continue")
        return 0
    finally:
        client.close()


async def _continue_run(client, record, args: argparse.Namespace, workspace: Path) -> int:
    """继续该 run。

    **不自动重跑**是本条的硬要求（spec §128、checklist「不自动重跑」）：继续只是往
    会话投一个新的用户回合，此前已经执行过的工具不会再被执行一次。这里额外把「可能
    已经生效、但结果没落盘」的调用点列出来——那是崩溃截断处最危险的一类，只看不执行。
    """
    session_dir = workspace / ".codeforge" / "sessions" / record.session_id

    pending = _pending_confirmations(session_dir)
    if pending:
        print(
            f"⚠️ 该会话有 {len(pending)} 个调用**可能已经生效**、但结果没落盘"
            "（继续不会自动重跑它们）："
        )
        for item in pending:
            hint = _input_hint(item.tool_input)
            print(f"  - {item.tool_name}{': ' + hint if hint else ''}")
            print(f"    tool_use_id = {item.tool_use_id}")
            if item.summary:
                print(f"    上次返回：{item.summary[:100]}")
        print("  需要重跑请自己判断后手动发起——写操作重放未必等价（追加、并发、外部副作用）。")
    else:
        print("该会话没有「已执行但结果未落盘」的调用，从这里继续是安全的。")

    entries = _read_journal_tail(session_dir)
    if entries:
        print(f"\n最近 {len(entries)} 条副作用记录（只读，不会重放）：")
        for e in entries:
            # journal 存的是**工具返回内容**；写类工具成功时常常是空串
            # （实测 write_file 就是），空尾的「- write_file: 」没有信息量，
            # 退回显示 tool_use_id，好歹能拿去和上面那段、和会话对上号。
            detail = str(e.summary)[:100] or e.tool_use_id
            print(f"  - {e.tool_name}: {detail}")

    from core.host import RunStatus

    if record.status is RunStatus.INTERRUPTED:
        print(f"\n上一个 run {record.id} 是 interrupted（通常是被强杀），会话内容仍完整。")

    # 首期是「单活跃 run」：host 只承载它自己那条 run。如果用户给的是历史 run，
    # 继续实际上发生在当前活跃 run 上——这必须说出来，不能默默跨 run/跨会话执行。
    live_run_id = client.run_id
    if live_run_id and live_run_id != record.id:
        live = await client.get_run()
        print(
            f"\n注意：{record.id} 不是当前活跃 run。host 正在承载的是 {live_run_id}"
            f"（session {live.get('session_id', '?')}），本次继续会在**它**上面执行。"
        )

    prompt = args.prompt.strip() or "继续"
    print(f"\n投递续跑输入：{prompt!r}")
    await client.send_message(prompt)

    print("-" * 60)
    try:
        async for event in client.events():
            name = event.get("event")
            data = event.get("data") or {}
            if name == "TextDelta":
                print(data.get("text", ""), end="", flush=True)
            elif name == "ToolCallStarted":
                print(f"\n[工具] {data.get('name')} 开始", flush=True)
            elif name == "ToolCallFinished":
                mark = "完成" if data.get("success") else "失败"
                print(f"[工具] {data.get('name')} {mark}（{data.get('duration_ms', 0)}ms）", flush=True)
            elif name == "AgentFinished":
                print(f"\n\n回合结束（{data.get('iterations', 0)} 轮，{data.get('elapsed_s', 0)}s）")
                return 0
            elif name == "AgentError":
                print(f"\n\n回合出错：{data.get('message') or data.get('code')}")
                return 1
    except KeyboardInterrupt:
        print("\n（已断开；host 仍在运行）")
        return 130
    finally:
        client.close()
    print("\n（事件流结束）")
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    """`codeforge attach <run_id> [--continue]`。"""
    import asyncio

    from core.host import RunStore

    run_id = args.target.strip()
    if not run_id:
        print("用法：codeforge attach <run_id> [--continue]")
        print("用 `codeforge runs` 看有哪些 run。")
        return 2

    workspace = Path.cwd()
    record = RunStore(workspace).get(run_id)
    if record is None:
        # 先离线查存在性：id 写错时不必先起 host 才知道
        print(f"找不到 run {run_id}。用 `codeforge runs` 看有哪些。")
        return 2

    _print_run_detail(record, workspace)
    try:
        return asyncio.run(_attach_async(workspace, record, args))
    except KeyboardInterrupt:
        print("\n（已中断）")
        return 130


def _resolve_host_settings(args: argparse.Namespace) -> tuple[str, int]:
    """算出 host 的 (无人值守策略, 端口)：CLI 显式传的优先，否则取 config。

    读取与默认档都在 `config.loader.load_host_config`（不抛异常，读不出来给默认档），
    TUI 的内嵌回落走同一处，避免两处默认值漂移。
    """
    from config.loader import load_host_config

    cfg = load_host_config("config.yaml")
    policy = getattr(args, "unattended_policy", None) or cfg.unattended_policy
    port_arg = getattr(args, "port", None)
    port = int(cfg.port) if port_arg is None else int(port_arg)
    return policy, port


def _cmd_host(args: argparse.Namespace) -> int:
    """`codeforge host` —— 前台启动会话宿主。"""
    import asyncio
    import signal

    from core.host import SessionLockedError
    from core.host.server import HostAlreadyRunningError, start_host

    policy, port = _resolve_host_settings(args)

    async def _serve() -> int:
        provider = _primary_provider()
        try:
            server = await start_host(
                provider=provider,
                workspace=Path.cwd(),
                unattended_policy=policy,
                port=port,
            )
        except HostAlreadyRunningError as e:
            print(f"该工作区已有活跃 run，不能重复起 host：{e}")
            print("查看：codeforge runs")
            return 1
        except SessionLockedError as e:
            print(f"会话已被其他进程占用：{e}")
            return 1

        print(f"host 已在监听 127.0.0.1:{server.port}")
        print(f"run_id = {server.run.id}")
        print(f"session = {server.run.session_id}")
        print(f"无人值守策略 = {policy}（ask 级工具决策按此代答）")
        print("另一个终端可用 `codeforge runs` / `codeforge attach <run_id>`；Ctrl-C 停止。")

        # Ctrl-C 走"请求优雅关闭"而不是让 asyncio 取消等待：取消会把 run 落成
        # interrupted，而用户主动停 host 是正常结束（completed）。Windows 上
        # loop.add_signal_handler 不可用，所以直接装 SIGINT 处理器 + 线程安全回调。
        loop = asyncio.get_running_loop()
        previous = signal.getsignal(signal.SIGINT)

        def _on_sigint(*_args: object) -> None:
            print("\n收到 Ctrl-C，正在关闭 host …")
            loop.call_soon_threadsafe(server.request_shutdown)

        signal.signal(signal.SIGINT, _on_sigint)
        try:
            await server.wait_closed()
        finally:
            signal.signal(signal.SIGINT, previous)
        print("host 已停止。")
        return 0

    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:
        print("\nhost 已停止。")
        return 0


def main() -> None:
    """程序入口：加载配置 → 选 provider → 启动对话。"""
    # Windows: 强制 UTF-8 编码以支持中文 IME 输入
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001, S110 —— 部分流可能不支持 reconfigure，忽略
            pass

    # 可观测性：Logs / Metrics / Traces 初始化(未启用/无依赖时安全 no-op)
    try:
        from core.observability import ensure_initialized

        ensure_initialized()
    except Exception:  # noqa: BLE001, S110 —— 可观测性失败绝不阻塞启动
        pass

    args = _parse_args()

    if args.command == "host":
        sys.exit(_cmd_host(args))
    if args.command == "runs":
        sys.exit(_cmd_runs())
    if args.command == "attach":
        sys.exit(_cmd_attach(args))
    if args.command:
        print(f"未知子命令：{args.command!r}")
        print("可用：host / runs / attach <run_id>")
        sys.exit(2)

    if args.team_member:
        # pane 队友子进程：不启动 TUI，跑自治循环
        from core.team.team_member import run_team_member_entry

        run_team_member_entry(args)
        return

    from tui.app import run

    run(task=args.task, loop=args.loop, host=args.host)


if __name__ == "__main__":
    main()
