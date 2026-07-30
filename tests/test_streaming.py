# ruff: noqa: I001
from types import SimpleNamespace
from typing import Any, cast
import unittest

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.conversation import ConversationService
from nonebot_plugin_llmchat.output_protocol import NO_REPLY
from nonebot_plugin_llmchat.state import StateStore
from nonebot_plugin_llmchat.streaming import StreamedMessageBuilder, StreamingReplyEmitter


def make_chunk(*, content=None, tool_calls=None, reasoning=None, usage=None):
    if content is None and tool_calls is None and reasoning is None:
        return SimpleNamespace(choices=[], usage=usage)
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning,
        images=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def tool_delta(index, *, tool_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=tool_id, type="function", function=function)


class StreamingEmitterTests(unittest.IsolatedAsyncioTestCase):
    async def test_emits_as_soon_as_a_text_line_is_complete(self):
        emitted = []

        async def emit(segment):
            emitted.append(segment)

        emitter = StreamingReplyEmitter(emit)
        await emitter.feed("第一")
        assert emitted == []
        await emitter.feed("条\n第二")
        assert emitted == ["第一条"]
        await emitter.feed("条")
        await emitter.finish()
        assert emitted == ["第一条", "第二条"]

    async def test_hides_tagged_reasoning_before_streaming_visible_content(self):
        emitted = []

        async def emit(segment):
            emitted.append(segment)

        emitter = StreamingReplyEmitter(emit)
        await emitter.feed("<thi")
        await emitter.feed("nk>secret</think>回答一\n")
        assert emitted == ["回答一"]
        await emitter.feed("回答二")
        await emitter.finish()
        assert emitted == ["回答一", "回答二"]

    async def test_fenced_code_waits_for_closing_fence_and_emits_once(self):
        emitted = []

        async def emit(segment):
            emitted.append(segment)

        emitter = StreamingReplyEmitter(emit)
        await emitter.feed("我来写\n```python\nprint(1)")
        assert emitted == ["我来写"]
        await emitter.feed("\nprint(2)\n``")
        assert emitted == ["我来写"]
        await emitter.feed("`")
        assert emitted == ["我来写", "print(1)\nprint(2)"]
        await emitter.finish()
        assert emitted == ["我来写", "print(1)\nprint(2)"]

    async def test_fenced_long_text_waits_for_closing_fence_and_emits_once(self):
        emitted = []

        async def emit(segment):
            emitted.append(segment)

        emitter = StreamingReplyEmitter(emit)
        await emitter.feed("```text\n第一段。\n\n第二")
        assert emitted == []
        await emitter.feed("段。\n``")
        assert emitted == []
        await emitter.feed("`")
        assert emitted == ["第一段。\n\n第二段。"]
        await emitter.finish()
        assert emitted == ["第一段。\n\n第二段。"]

    async def test_no_reply_marker_is_not_emitted(self):
        emitted = []

        async def emit(segment):
            emitted.append(segment)

        emitter = StreamingReplyEmitter(emit)
        await emitter.feed(NO_REPLY)
        await emitter.finish()
        assert emitted == []


class StreamedMessageBuilderTests(unittest.TestCase):
    def test_reassembles_content_reasoning_and_tool_call_deltas(self):
        builder = StreamedMessageBuilder()
        first = tool_delta(0, tool_id="call-1", name="se", arguments='{"query":')
        second = tool_delta(0, name="arch", arguments='"hello"}')
        assert builder.add_chunk(make_chunk(content="hi", tool_calls=[first], reasoning="think ")) == "hi"
        assert builder.add_chunk(make_chunk(content="!", tool_calls=[second], reasoning="more")) == "!"
        usage = SimpleNamespace(total_tokens=12)
        builder.add_chunk(make_chunk(usage=usage))

        message = builder.build()
        assert message.content == "hi!"
        assert message.reasoning_content == "think more"
        assert message.tool_calls[0].id == "call-1"
        assert message.tool_calls[0].function.name == "search"
        assert message.tool_calls[0].function.arguments == '{"query":"hello"}'
        assert builder.usage.total_tokens == 12


class FakeAsyncStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self.chunks:
            yield chunk


class FakeStreamingCompletions:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeAsyncStream(self.chunks)


class ConversationStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_streams_segments_and_returns_full_message(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x", stream=True)
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        sent = []

        async def sender(message):
            sent.append(str(message))

        service = ConversationService(config, StateStore(config), {"bot"}, sender)
        completions = FakeStreamingCompletions(
            [
                make_chunk(content="第一条\n第"),
                make_chunk(content="二条"),
            ]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        result = await service._complete(
            cast(Any, client),
            {"model": "test"},
            [{"role": "user", "content": "hello"}],
            stream=True,
        )

        assert sent == ["第一条", "第二条"]
        assert result.streamed
        assert result.message.content == "第一条\n第二条"
        assert completions.calls[0]["stream"] is True


class SequenceStreamingCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeAsyncStream(self.responses.pop(0))


class RecordingMCPClient:
    def __init__(self):
        self.calls = []

    def get_friendly_name(self, name):
        return name

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        return "tool result"


class StreamingToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_tool_call_is_reassembled_before_final_streamed_reply(self):
        preset = PresetConfig(
            name="test",
            api_base="x",
            api_key="x",
            model_name="x",
            stream=True,
            support_mcp=True,
        )
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        sent = []

        async def sender(message):
            sent.append(str(message))

        service = ConversationService(config, StateStore(config), {"bot"}, sender)
        first_tool_delta = tool_delta(
            0,
            tool_id="call-1",
            name="mcp__demo__search",
            arguments='{"query":',
        )
        second_tool_delta = tool_delta(0, arguments='"hello"}')
        completions = SequenceStreamingCompletions(
            [
                [
                    make_chunk(tool_calls=[first_tool_delta]),
                    make_chunk(tool_calls=[second_tool_delta]),
                ],
                [
                    make_chunk(content="查到了\n"),
                    make_chunk(content="结果如下"),
                ],
            ]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        mcp_client = RecordingMCPClient()
        transcript = [{"role": "user", "content": "search"}]
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

        assert completion.streamed
        assert completion.message.content == "查到了\n结果如下"
        assert mcp_client.calls[0][0] == "mcp__demo__search"
        assert mcp_client.calls[0][1] == {"query": "hello"}
        assert sent == ["正在使用mcp__demo__search", "查到了", "结果如下"]
        assert len(completions.calls) == 2
        assert all(call["stream"] is True for call in completions.calls)
