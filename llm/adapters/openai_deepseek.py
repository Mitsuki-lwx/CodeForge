"""DeepSeek 的 OpenAI 兼容协议适配器。

相对 openai_base 的差异（DeepSeek thinking 特有）：
  - thinking 模式要求把 assistant 的推理文本以顶层 `reasoning_content` 回传，
    否则报 `The reasoning_content in the thinking mode must be passed back...`。

当前 `_to_openai_wire` 已统一把 `APIMessage.reasoning` → 顶层 `reasoning_content`
（对朴素 OpenAI 亦无害）。本类保留扩展点：后续若 DeepSeek 出现专属行为
（端点路径、额外 body 字段、非 thinking 下抑制回传等），在此叠加即可，
不动 base 的共性逻辑。

**注册**：本模块声明两条规则（见文件末尾的装饰器）——"vendor 精确"与
"端点/模型名启发式"。选择逻辑不再写在 `llm/protocol.py` 里。
"""

from __future__ import annotations

from llm.adapters.openai_base import OpenAIConversationAdapter
from llm.adapters.registry import register_adapter


def _looks_like_deepseek(
    protocol: str,
    vendor: str | None,
    model: str,
    base_url: str | None,
) -> bool:
    """端点 / 模型名启发式：这个 openai 兼容端点是不是 DeepSeek 上游。

    **保留旧 `llm/protocol.py` 里的一处细节**：未知 vendor 不短路 ——
    vendor 既不是 deepseek、也不是 openai（比如留空或写了别的）时，
    仍然继续检查 base_url / model。改掉这个语义会让现有配置的选择结果变化。
    """
    if (vendor or "").lower() == "deepseek":
        return True
    host = (base_url or "").lower()
    return "deepseek" in host or model.lower().startswith("deepseek")


@register_adapter(
    "openai",
    vendor="deepseek",  # 精确匹配：优先级高于下面的启发式
    priority=900,
    client="llm.openai_client.OpenAIClient",
)
@register_adapter(
    "openai",
    predicate=_looks_like_deepseek,  # 端点/模型名像 deepseek 时也用它
    priority=800,
    client="llm.openai_client.OpenAIClient",
)
class DeepSeekConversationAdapter(OpenAIConversationAdapter):
    """DeepSeek 上游：OpenAI 兼容 wire + thinking reasoning 回传。"""

    # 预留：如需覆盖端点/额外字段，在这里 subclass 覆写 build_request / build_url。
