# ruff: noqa: I001
from types import SimpleNamespace
from typing import Any, cast
import unittest

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.context_manager import ContextManager
from nonebot_plugin_llmchat.conversation import ConversationService
from nonebot_plugin_llmchat.memory import ConversationTurn, restore_turns
from nonebot_plugin_llmchat.state import StateStore


def make_turn(number: int, *, with_tool: bool = False) -> ConversationTurn:
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"user-{number}"}]
    if with_tool:
        messages.extend(
            [
                {"role": "assistant", "content": None, "tool_calls": [{"id": f"call-{number}"}]},
                {"role": "tool", "tool_call_id": f"call-{number}", "content": "result"},
            ]
        )
    messages.append({"role": "assistant", "content": f"assistant-{number}"})
    return ConversationTurn(messages)


class FakeCompressionCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="压缩后的长期记忆")
        usage = SimpleNamespace(prompt_tokens=500, prompt_tokens_details=SimpleNamespace(cached_tokens=0))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def test_old_flat_history_is_grouped_without_splitting_tools(self):
        messages = [
            {"role": "assistant", "content": "orphan"},
            *make_turn(1, with_tool=True).messages,
            *make_turn(2).messages,
        ]

        turns = restore_turns(messages)

        assert len(turns) == 2
        assert [message["role"] for message in turns[0].messages] == ["user", "assistant", "tool", "assistant"]
        assert turns[1].messages[0]["content"] == "user-2"

    async def test_usage_high_watermark_compresses_complete_old_turns(self):
        preset = PresetConfig(
            name="test",
            api_base="x",
            api_key="x",
            model_name="x",
            context_window=4096,
            max_tokens=512,
        )
        config = ScopedConfig(
            api_presets=[preset],
            default_preset="test",
            context_safety_tokens=0,
            context_compression_trigger_ratio=0.8,
            context_compression_target_ratio=0.5,
            compression_keep_recent_turns=1,
        )
        states = StateStore(config)
        state = states.group_states[1]
        state.turns.extend([make_turn(1, with_tool=True), make_turn(2), make_turn(3), make_turn(4)])
        state.last_prompt_tokens = 3200
        state.usage_preset_name = "test"
        completions = FakeCompressionCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        service = ConversationService(config, states, {"bot"}, cast(Any, lambda _: None))

        compressed = await service.context_manager.maybe_compress(cast(Any, client), preset, state, 4096)

        assert compressed
        assert state.memory_summary == "压缩后的长期记忆"
        assert len(state.turns) == 2
        assert state.turns[0].messages[0]["content"] == "user-3"
        assert state.last_prompt_tokens is None
        assert "stream" not in completions.calls[0]

    def test_records_prompt_and_cached_token_usage(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        state = StateStore(config).group_states[1]
        service = ConversationService(config, StateStore(config), {"bot"}, cast(Any, lambda _: None))
        usage = SimpleNamespace(prompt_tokens=1200, prompt_tokens_details=SimpleNamespace(cached_tokens=768))

        service.context_manager.record_usage(state, preset, usage)

        assert state.last_prompt_tokens == 1200
        assert state.last_cached_tokens == 768
        assert state.usage_preset_name == "test"

    def test_context_window_is_read_from_common_provider_metadata(self):
        assert ContextManager.find_context_window({"context_length": 128000}) == 128000
        assert ContextManager.find_context_window({"top_provider": {"context_length": 64000}}) == 64000
        assert ContextManager.find_context_window({"id": "model"}) is None

    def test_build_messages_keeps_summary_before_complete_recent_turns(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        state = StateStore(config).group_states[1]
        state.memory_summary = "old facts"
        state.turns.extend([make_turn(1, with_tool=True), make_turn(2)])

        messages = ContextManager.build_messages("system prompt", state)

        assert [message["role"] for message in messages] == [
            "system",
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "user",
            "assistant",
        ]
        assert "old facts" in messages[1]["content"]

    def test_context_overflow_detection(self):
        assert ContextManager.is_context_overflow(RuntimeError("maximum context length exceeded"))
        assert not ContextManager.is_context_overflow(RuntimeError("rate limit exceeded"))

    def test_streaming_requests_can_require_usage_chunk(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x", stream=True)
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        service = ConversationService(config, StateStore(config), {"bot"}, cast(Any, lambda _: None))

        request_config = service._build_request_config(preset)

        assert request_config["stream_options"] == {"include_usage": True}
        disabled = preset.model_copy(update={"stream_include_usage": False})
        assert "stream_options" not in service._build_request_config(disabled)

    async def test_failed_compression_does_not_remove_original_turns(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x", context_window=4096)
        config = ScopedConfig(
            api_presets=[preset],
            default_preset="test",
            compression_keep_recent_turns=1,
        )
        states = StateStore(config)
        state = states.group_states[1]
        state.turns.extend([make_turn(1), make_turn(2)])

        class EmptyCompletions:
            async def create(self, **_kwargs):
                message = SimpleNamespace(content="")
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(completions=EmptyCompletions()))
        service = ConversationService(config, states, {"bot"}, cast(Any, lambda _: None))

        error_message = ""
        try:
            await service.context_manager.compress(cast(Any, client), preset, state, context_window=4096, force=False)
        except RuntimeError as error:
            error_message = str(error)
        else:
            raise AssertionError("压缩结果为空时应抛出异常")

        assert "未返回摘要" in error_message
        assert len(state.turns) == 2
        assert state.memory_summary is None
