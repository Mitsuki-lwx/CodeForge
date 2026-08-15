"""DeepSeek 的 OpenAI 兼容协议适配器。

相对 openai_base 的差异（DeepSeek thinking 特有）：
  - thinking 模式要求把 assistant 的推理文本以顶层 `reasoning_content` 回传，
    否则报 `The reasoning_content in the thinking mode must be passed back...`。

当前 `_to_openai_wire` 已统一把 `APIMessage.reasoning` → 顶层 `reasoning_content`
（对朴素 OpenAI 亦无害）。本类保留扩展点：后续若 DeepSeek 出现专属行为
（端点路径、额外 body 字段、非 thinking 下抑制回传等），在此叠加即可，
不动 base 的共性逻辑。
"""

from __future__ import annotations

from llm.adapters.openai_base import OpenAIConversationAdapter


class DeepSeekConversationAdapter(OpenAIConversationAdapter):
    """DeepSeek 上游：OpenAI 兼容 wire + thinking reasoning 回传。"""

    # 预留：如需覆盖端点/额外字段，在这里 subclass 覆写 build_request / build_url。
