from types import SimpleNamespace
from typing import Any, cast
import unittest

# ruff: noqa: I001
import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.conversation import ConversationService
from nonebot_plugin_llmchat.state import StateStore


class FakeToolCall:
    def __init__(self, name="mcp__demo__search"):
        self.id = "call-1"
        self.function = SimpleNamespace(name=name, arguments='{"query":"same"}')

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if "tools" not in kwargs:
            message = FakeMessage("final answer")
        else:
            message = FakeMessage(tool_calls=[FakeToolCall()])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())


class FakeMCPClient:
    def __init__(self):
        self.call_count = 0

    def get_friendly_name(self, _name):
        return "测试工具"

    async def call_tool(self, *_args, **_kwargs):
        self.call_count += 1
        return "result"


class FakeStickerService:
    def __init__(self):
        self.call_count = 0

    @staticmethod
    def is_sticker_tool(name):
        return name == "sticker__send"

    async def call_tool(self, _name, _arguments, _candidates, sender):
        self.call_count += 1
        await sender("sticker sent")
        return "已找到并发送表情包"


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_tool_calls_are_bounded_and_forced_to_finish(self):
        preset = PresetConfig(
            name="test",
            api_base="https://example.invalid/v1",
            api_key="test",
            model_name="test",
            support_mcp=True,
        )
        config = ScopedConfig(
            api_presets=[preset],
            default_preset="test",
            max_tool_rounds=3,
            max_repeated_tool_calls=2,
        )
        sent = []

        async def sender(message):
            sent.append(message)

        service = ConversationService(config, StateStore(config), {"bot"}, sender)
        client = FakeClient()
        mcp_client = FakeMCPClient()
        transcript = [{"role": "user", "content": "hello"}]
        completion = await service._run_tool_loop(
            client=cast(Any, client),
            preset=preset,
            mcp_client=cast(Any, mcp_client),
            request_config={"model": "test", "tools": [{}]},
            messages=[{"role": "system", "content": "system"}],
            transcript=transcript,
            event=cast(Any, SimpleNamespace(self_id=1)),
            is_group=False,
        )

        assert completion.message.content == "final answer"
        assert mcp_client.call_count == 2
        assert len(client.chat.completions.calls) == 5
        assert "tools" not in client.chat.completions.calls[-1]
        assert "达到上限" in transcript[-1]["content"]

    async def test_sticker_tools_work_without_mcp_and_have_no_tool_notice_or_repeat_limit(self):
        preset = PresetConfig(
            name="test",
            api_base="https://example.invalid/v1",
            api_key="test",
            model_name="test",
            support_image=True,
        )
        config = ScopedConfig(
            api_presets=[preset],
            default_preset="test",
            max_tool_rounds=3,
            max_repeated_tool_calls=1,
        )
        sent = []

        async def sender(message):
            sent.append(str(message))

        sticker_service = FakeStickerService()
        service = ConversationService(
            config,
            StateStore(config),
            {"bot"},
            sender,
            sticker_service=cast(Any, sticker_service),
        )
        client = FakeClient()
        client.chat.completions.create = self._sticker_completion(client.chat.completions.calls)
        transcript = [{"role": "user", "content": "hello"}]

        await service._run_tool_loop(
            client=cast(Any, client),
            preset=preset,
            mcp_client=cast(Any, FakeMCPClient()),
            request_config={"model": "test", "tools": [{}]},
            messages=[{"role": "system", "content": "system"}],
            transcript=transcript,
            event=cast(Any, SimpleNamespace(self_id=1)),
            is_group=False,
        )

        assert sticker_service.call_count == 3
        assert sent == ["sticker sent", "sticker sent", "sticker sent"]
        assert all("正在使用" not in message for message in sent)
        assert [item["content"] for item in transcript if item["role"] == "tool"] == [
            "已找到并发送表情包",
            "已找到并发送表情包",
            "已找到并发送表情包",
        ]

    @staticmethod
    def _sticker_completion(calls):
        async def create(**kwargs):
            calls.append(kwargs)
            if "tools" not in kwargs:
                message = FakeMessage("final answer")
            else:
                message = FakeMessage(tool_calls=[FakeToolCall("sticker__send")])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        return create
