"""后台任务 / 队友「下钻与干预」的 TUI 粘合层。

渲染是纯函数（`core/task/view.py`）；这里只做：取任务 → 解析选择器 → 调引擎 / 排队。

**为什么"跑着就排队"放在这里而不是引擎里**：投递要发生在"任务跑完"那一刻，
若放进 `BackgroundTaskManager._runner` 的 `finally`，就得在收尾路径上新增 `await`
—— 正是上一轮踩过的坑（`docs/spec_teammate_status.md` §9，0.03ms 的 await 弄红 2 条既有测试）。
排队本质是**交互层语义**（"人在它跑完之前说的话"），放这里爆炸半径最小。

形态选择（为什么不是方向键整屏面板）：`docs/spec_teammate_inspect.md` §4。
"""

from __future__ import annotations

import logging
from typing import Any

from core.task.manager import TaskBusyError, TaskNotFoundError, TaskStatus
from core.task.view import (
    DEFAULT_TAIL,
    display_name,
    render_agent_list,
    render_transcript,
    resolve_selector,
)

logger = logging.getLogger(__name__)

_SEL_USAGE = "序号 / id / 名字（/agents 看列表）"


def tasks_of(app: Any) -> list:
    """当前后台任务快照（按 start_time 升序）；拿不到就返回空。"""
    mgr = getattr(app, "task_mgr", None)
    if mgr is None:
        return []
    try:
        return list(mgr.list())
    except Exception:  # noqa: BLE001 —— 下钻功能不该拖垮主流程
        return []


def _queue(app: Any) -> dict[str, list[str]]:
    """排队中的消息（task_id → 消息列表）。容器缺失时就地补一个。"""
    q = getattr(app, "pending_agent_messages", None)
    if isinstance(q, dict):
        return q
    q = {}
    try:
        app.pending_agent_messages = q  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001 —— 补不上就退化成"不排队"，绝不抛
        logger.debug("cannot attach pending_agent_messages to %r: %s", type(app), e)
    return q


def list_lines(app: Any, *, show_all: bool = False) -> list[str]:
    """`/agents` 的列表。"""
    queued = {k: len(v) for k, v in _queue(app).items()}
    return render_agent_list(tasks_of(app), show_all=show_all, queued=queued)


def show_lines(
    app: Any, sel: str, *, tail: int = DEFAULT_TAIL, full: bool = False
) -> list[str]:
    """`/agents show` 的 transcript。"""
    r = resolve_selector(
        sel, tasks_of(app), usage=f"缺少选择器（用法：/agents show <{_SEL_USAGE}>）"
    )
    if not r.ok:
        return [f"x {r.error}"]
    return render_transcript(r.task, index=r.index, tail=tail, full=full)


async def stop(app: Any, sel: str) -> str:
    """停掉一个任务（同时丢弃它排队中的消息 —— 人都把它停了，就别再续派）。"""
    mgr = getattr(app, "task_mgr", None)
    if mgr is None:
        return "x 没有后台任务管理器"

    r = resolve_selector(
        sel, tasks_of(app), usage=f"缺少选择器（用法：/agents stop <{_SEL_USAGE}>）"
    )
    if not r.ok:
        return f"x {r.error}"

    dropped = len(_queue(app).pop(r.task.id, []) or [])
    try:
        found = await mgr.stop(r.task.id)
    except Exception as e:  # noqa: BLE001 —— 停止失败要报出来，不能静默
        return f"x 停止失败：{e}"
    if not found:
        return f"x 未找到任务 {r.task.id}"

    text = f"已发出停止请求：#{r.index} {display_name(r.task)}"
    if dropped:
        text += f"（同时丢弃排队的 {dropped} 条消息）"
    return text


async def tell(app: Any, sel: str, message: str) -> str:
    """对一个任务说话：跑着 → 排队；已停下 → 立即续派。"""
    mgr = getattr(app, "task_mgr", None)
    if mgr is None:
        return "x 没有后台任务管理器"

    r = resolve_selector(
        sel,
        tasks_of(app),
        usage=f"缺少选择器（用法：/agents tell <{_SEL_USAGE}> <消息>）",
    )
    if not r.ok:
        return f"x {r.error}"

    label = f"#{r.index} {display_name(r.task)}"
    if getattr(r.task, "status", None) == TaskStatus.RUNNING:
        _queue(app).setdefault(r.task.id, []).append(message)
        return f"任务在跑，消息已排队（跑完自动续派）：{label}"

    try:
        await mgr.send_message_to(r.task.id, message)
    except TaskNotFoundError:
        return f"x 任务已不存在：{label}"
    except TaskBusyError:
        return f"x 任务正在跑，无法续派：{label}"
    except Exception as e:  # noqa: BLE001 —— 续派失败要报出来
        return f"x 续派失败：{e}"
    return f"已续派给 {label}"


async def deliver_pending(app: Any, task_id: str) -> str | None:
    """任务落下时投递排队消息（`_consume_task_done` 调）。

    - `COMPLETED` → 多条**合并成一条**续派（不为一个任务连开两轮）
    - `FAILED` / `CANCELLED` / 任务已不存在 → 丢弃并说清楚没投出去
    - 没有排队消息 → 返回 `None`（调用方据此判断"要不要多说一句"）
    """
    queue = _queue(app)
    msgs = queue.pop(task_id, None)
    if not msgs:
        return None

    mgr = getattr(app, "task_mgr", None)
    bt = mgr.get(task_id) if mgr is not None else None
    label = f"{display_name(bt)}" if bt is not None else task_id

    if bt is None:
        return f"x 排队给 {task_id} 的 {len(msgs)} 条消息未投递（任务已不存在）"
    if getattr(bt, "status", None) != TaskStatus.COMPLETED:
        return f"x 排队给 {label} 的 {len(msgs)} 条消息未投递（任务已结束，非正常完成）"

    try:
        await mgr.send_message_to(task_id, "\n\n".join(msgs))
    except Exception as e:  # noqa: BLE001 —— 投递失败要说出来，否则消息无声消失
        return f"x 排队消息投递失败（{e}）：{label}"
    return f"已把排队的 {len(msgs)} 条消息续派给 {label}"


__all__ = [
    "deliver_pending",
    "list_lines",
    "show_lines",
    "stop",
    "tasks_of",
    "tell",
]
