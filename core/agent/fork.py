"""Fork 路径辅助函数。

提供 Fork Boilerplate 常量、build_forked_messages / is_fork_context，
用于 Fork 式子 Agent 的消息构造与嵌套阻断检测。
"""

from __future__ import annotations

import copy

from conversation.message import Message, MessageRole

# Fork 子 Agent 首条 user 消息中的标记标签。
# 用于 is_fork_context 扫描（QuerySource 失效时的兜底检测）。
FORK_BOILERPLATE_TAG = "<fork_boilerplate>"

# Fork 子 Agent 首条 user 消息的前缀，约束其行为边界。
FORK_BOILERPLATE = """<fork_boilerplate>
You are a forked worker process. You are NOT the main agent.
Rules (non-negotiable):
1. Do NOT fork again (calling the Agent tool will be blocked).
2. Do NOT chat, ask questions, or request confirmation.
3. Use tools directly: read files, search code, make changes.
4. Stay strictly within your assigned task scope.
5. Final report must start with "Scope:" and be under 500 words.
</fork_boilerplate>

"""


def build_forked_messages(
    parent_msgs: list[Message],
    task: str,
) -> list[Message]:
    """克隆父对话到 Fork 子对话，处理悬空 tool_use，追加 Boilerplate + task。

    行为：
      1. 深拷贝 parent_msgs（所有 Message + 内部字段）
      2. 扫描末尾 assistant 消息的 tool_use_id：
         如果对应的 tool_result 消息缺失，
         生成一条 placeholder tool_result（内容为 "[forked, skipped]"）
      3. 追加一条 user 消息，内容 = FORK_BOILERPLATE + task

    Args:
        parent_msgs: 父对话的完整消息列表。
        task: 子 Agent 的任务描述。

    Returns:
        新消息列表，可直接用 ConversationManager.replace_history 装载。
    """
    cloned = copy.deepcopy(list(parent_msgs))

    # ── 扫描未配对的 tool_use ──
    # 收集所有 tool_result 对应的 tool_use_id
    result_ids: set[str] = set()
    for m in cloned:
        if m.role == MessageRole.USER and m.tool_use_id:
            result_ids.add(m.tool_use_id)

    # 收集末尾需要占位填充的 tool_use
    unpaired_ids: list[str] = []
    for m in reversed(cloned):
        if m.role == MessageRole.ASSISTANT and m.tool_use_id and m.tool_name:
            if m.tool_use_id not in result_ids:
                unpaired_ids.append(m.tool_use_id)
        else:
            # 遇到非 tool_use 的 assistant / 或 user 消息 → 停止反向扫描
            break

    # ── 为每个未配对 tool_use 生成 placeholder tool_result ──
    for tid in reversed(unpaired_ids):
        cloned.append(
            Message(
                role=MessageRole.USER,
                content="[forked, skipped]",
                tool_use_id=tid,
            )
        )

    # ── 追加 Fork Boilerplate + 任务 ──
    cloned.append(
        Message(
            role=MessageRole.USER,
            content=FORK_BOILERPLATE + task,
        )
    )

    return cloned


def is_fork_context(msgs: list[Message]) -> bool:
    """判定消息历史是否来自 Fork 路径。

    扫描所有 user 消息内容，查找 <fork_boilerplate> 标记。
    QuerySource 检测的兜底机制——caller 链丢失时靠此检测。

    Args:
        msgs: 消息列表。

    Returns:
        True 如果历史中包含 Fork Boilerplate 标记。
    """
    for m in msgs:
        if m.role == MessageRole.USER and FORK_BOILERPLATE_TAG in (m.content or ""):
            return True
    return False
