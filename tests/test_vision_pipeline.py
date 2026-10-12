"""图像回灌 —— 让模型**看见**工具产出的截图。

覆盖三件事：
1. **内部形态**：`ToolResult.images` → `Message.images` → tool_result 块数组；
2. **两个协议的翻译**：Anthropic 走 tool_result 的 image 块；OpenAI 的
   `role=tool` 只能放字符串，图必须另起 user 消息（`image_url` data URI）；
3. **不该带图时不带**：provider 没开 `vision` 时图像必须被丢掉，并在文本里
   说明"图还在磁盘上"—— 否则文本模型收到图像块会直接 400。
"""

from __future__ import annotations

import base64

import pytest

from config.model import ProviderConfig
from conversation.manager import ConversationManager
from conversation.message import (
    APIMessage,
    Message,
    MessageRole,
    make_image_block,
    make_tool_result_block,
)
from core.tool.interface import Tool
from llm.adapters.anthropic import AnthropicAdapter
from llm.adapters.openai_base import _split_tool_result_content, _to_openai_wire
from llm.adapters.registry import resolve_adapter_class

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKE").decode("ascii")
IMG = [{"media_type": "image/png", "data": PNG_B64}]


class TestBlockShape:
    def test_no_images_keeps_plain_string(self):
        """没有图像时形状**逐字不变** —— 绝大多数工具结果走这条路。"""
        blk = make_tool_result_block("c1", "hello")
        assert blk == {"type": "tool_result", "tool_use_id": "c1", "content": "hello"}

    def test_with_images_content_becomes_block_array(self):
        blk = make_tool_result_block("c1", "shot", IMG)
        assert blk["type"] == "tool_result"
        assert isinstance(blk["content"], list)
        assert blk["content"][0] == {"type": "text", "text": "shot"}
        assert blk["content"][1] == make_image_block("image/png", PNG_B64)

    def test_empty_text_with_images_still_valid(self):
        """只有图没有文字时不能产生空 text 块（有些端点拒绝空块）。"""
        blk = make_tool_result_block("c1", "", IMG)
        assert [b["type"] for b in blk["content"]] == ["image"]


class TestConversationRoundTrip:
    def test_add_tool_result_carries_images(self):
        conv = ConversationManager()
        conv.add_tool_use("c1", "browser_screenshot", {})
        conv.add_tool_result("c1", "saved", IMG)
        msgs = conv.messages
        assert msgs[-1].images == IMG

    def test_to_api_format_emits_block_array(self):
        conv = ConversationManager()
        conv.add_tool_result("c1", "saved", IMG)
        api, _ = conv.to_api_format()
        blk = api[-1].content[0]
        assert blk["type"] == "tool_result"
        assert any(b.get("type") == "image" for b in blk["content"])

    def test_no_images_path_unchanged(self):
        conv = ConversationManager()
        conv.add_tool_result("c1", "plain")
        api, _ = conv.to_api_format()
        assert api[-1].content[0]["content"] == "plain"


class TestAnthropicWire:
    def _body(self, messages):
        cfg = ProviderConfig(name="a", protocol="anthropic", model="m", api_key="k")
        adapter = AnthropicAdapter(cfg)
        import asyncio

        return asyncio.run(
            adapter.build_request(
                messages, system_prompt="", system_blocks=None, tools=None
            )
        )

    def test_tool_result_keeps_image_block(self):
        m = APIMessage(role="user", content=[make_tool_result_block("c1", "t", IMG)])
        blk = self._body([m])["messages"][0]["content"][0]
        assert blk["content"][1]["type"] == "image"
        assert blk["content"][1]["source"]["data"] == PNG_B64


class TestOpenAIWire:
    def test_tool_message_is_text_and_image_goes_to_user(self):
        """★ OpenAI 的 `role=tool` 不能带图 —— 图必须另起一条 user 消息。"""
        m = APIMessage(role="user", content=[make_tool_result_block("c1", "shot", IMG)])
        out = _to_openai_wire(m)
        assert out[0]["role"] == "tool"
        assert out[0]["content"] == "shot"  # 字符串，不是块数组
        assert out[1]["role"] == "user"
        parts = out[1]["content"]
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"

    def test_no_images_means_no_extra_message(self):
        m = APIMessage(role="user", content=[make_tool_result_block("c1", "shot")])
        out = _to_openai_wire(m)
        assert len(out) == 1
        assert out[0]["role"] == "tool"

    def test_split_helper_handles_all_shapes(self):
        assert _split_tool_result_content("plain") == ("plain", [])
        assert _split_tool_result_content(b"bytes") == ("bytes", [])
        text, imgs = _split_tool_result_content(
            [{"type": "text", "text": "a"}, {"type": "image", "source": {"data": "x"}}]
        )
        assert text == "a" and len(imgs) == 1
        # 畸形输入不该抛
        assert _split_tool_result_content(None)[0] == "None"


class TestVisionGate:
    """没开 `vision` 时图像**必须**被拦在执行层，不能发出去。"""

    def _agent(self, tmp_path, *, vision: bool = False, cfg: object | None = None):
        from core.agent.agent import Agent
        from core.agent.config import AgentConfig
        from core.tool.context import ExecutionContext
        from core.tool.registry import ToolRegistry

        if cfg is None:
            cfg = type(
                "_Cfg",
                (),
                {"model": "m", "context_window": 200000, "vision": vision},
            )()
        client = type("_Client", (), {"config": cfg})()
        return Agent(
            registry=ToolRegistry(),
            llm_client=client,
            exec_ctx=ExecutionContext(cwd=tmp_path, session_id="t"),
            conversation=ConversationManager(),
            config=AgentConfig(max_iterations=2),
        )

    def test_vision_off_by_default(self, tmp_path):
        assert self._agent(tmp_path)._vision_enabled() is False

    def test_vision_flag_is_honoured(self, tmp_path):
        assert self._agent(tmp_path, vision=True)._vision_enabled() is True

    def test_missing_attr_falls_back_to_off(self, tmp_path):
        """配置里没有 vision 字段（老配置）时按**不支持**处理（保守）。"""
        cfg = type("_OldCfg", (), {"model": "m", "context_window": 1})()
        assert self._agent(tmp_path, cfg=cfg)._vision_enabled() is False


class TestPersistence:
    def test_images_are_not_written_to_the_session_file(self):
        """图像**不落盘** —— 几百 KB 的 base64 会把会话 JSONL 撑爆。"""
        from core.archive.writer import serialize_message

        msg = Message(
            role=MessageRole.USER,
            content="shot",
            tool_use_id="c1",
            images=IMG,
        )
        data = serialize_message(msg)
        assert "images" not in data
        assert data["content"] == "shot"


class TestAdapterRouting:
    def test_openai_adapter_is_selected_for_openai_protocol(self):
        cls = resolve_adapter_class("openai", None, "gpt-4o", None)
        assert cls.__name__ == "OpenAIConversationAdapter"

    @pytest.mark.parametrize("protocol", ["anthropic", "openai"])
    def test_every_adapter_accepts_a_tool_result_with_images(self, protocol):
        """两个协议都不能在"带图的 tool_result"上崩。"""
        m = APIMessage(role="user", content=[make_tool_result_block("c1", "t", IMG)])
        if protocol == "anthropic":
            cfg = ProviderConfig(name="a", protocol="anthropic", model="m", api_key="k")
            AnthropicAdapter(cfg)
        else:
            out = _to_openai_wire(m)
            assert out


class _ShotTool(Tool):
    """假工具：返回一张图（模拟 browser_screenshot）。

    必须继承真 `Tool`：注册表会调 `validate_input`（schema 校验），
    鸭子类型的假工具会在那里炸。
    """

    timeout_seconds = 5.0
    max_retries = 0

    def __init__(self, images):
        self._images = images

    def name(self):
        return "shot"

    def description(self):
        return "fake"

    def input_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, context, input):
        from core.tool.result import ToolResult

        return ToolResult(
            success=True, data="截图已保存: /tmp/x.png", images=self._images
        )

    def is_read_only(self):
        return True

    def is_destructive(self):
        return False

    def is_concurrency_safe(self, input):
        return True

    def category(self):
        return "read"


class TestExecuteToolsIntegration:
    """★ 端到端：`_execute_tools` 回灌时按 vision 决定带不带图。"""

    async def _run(self, tmp_path, *, vision: bool):
        import asyncio

        from core.agent.agent import Agent
        from core.agent.config import AgentConfig
        from core.tool.context import ExecutionContext
        from core.tool.registry import ToolRegistry
        from llm.stream_events import ToolUse

        cfg = type(
            "_Cfg",
            (),
            {"model": "m", "context_window": 200000, "vision": vision},
        )()
        client = type("_Client", (), {"config": cfg})()

        registry = ToolRegistry()
        registry.register(_ShotTool(IMG))

        agent = Agent(
            registry=registry,
            llm_client=client,
            exec_ctx=ExecutionContext(cwd=tmp_path, session_id="t"),
            conversation=ConversationManager(),
            config=AgentConfig(max_iterations=2),
        )
        tu = ToolUse(id="c1", name="shot", input={})

        async def drain():
            async for _ev in agent._execute_tools([tu]):
                pass

        await asyncio.wait_for(drain(), timeout=10)
        return agent._conversation.messages[-1]

    @pytest.mark.asyncio
    async def test_images_are_attached_when_vision_on(self, tmp_path):
        msg = await self._run(tmp_path, vision=True)
        assert msg.images == IMG
        assert "未开启 vision" not in msg.content

    @pytest.mark.asyncio
    async def test_images_are_dropped_and_explained_when_vision_off(self, tmp_path):
        """★ 文本模型收到图像块会 400 —— 必须拦在执行层，且**说明**图还在磁盘上。"""
        msg = await self._run(tmp_path, vision=False)
        assert msg.images == []
        assert "未开启 vision" in msg.content
        assert "/tmp/x.png" in msg.content  # 路径仍在，人能找到图
