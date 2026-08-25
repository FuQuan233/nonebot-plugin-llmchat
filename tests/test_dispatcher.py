import asyncio
import unittest

# ruff: noqa: I001
import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.dispatcher import MessageDispatcher
from nonebot_plugin_llmchat.state import ChatState


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_enqueued_while_worker_runs_are_not_stranded(self):
        started = asyncio.Event()
        release = asyncio.Event()
        processed = []

        async def process(_context_id, _is_group, _state, event):
            processed.append(event)
            if event == "first":
                started.set()
                await release.wait()

        state = ChatState(preset_name="test", past_events_size=10)
        dispatcher = MessageDispatcher(process)
        await dispatcher.enqueue(1, True, state, "first")
        await asyncio.wait_for(started.wait(), timeout=1)
        await dispatcher.enqueue(1, True, state, "second")
        release.set()
        await asyncio.wait_for(state.queue.join(), timeout=1)

        assert processed, ["first", "second"]
        assert state.queue.empty()
        await dispatcher.shutdown()

    async def test_processor_failure_does_not_stop_the_queue(self):
        processed = []

        async def process(_context_id, _is_group, _state, event):
            processed.append(event)
            if event == "bad":
                raise RuntimeError("boom")

        state = ChatState(preset_name="test", past_events_size=10)
        dispatcher = MessageDispatcher(process)
        await dispatcher.enqueue(1, True, state, "bad")
        await dispatcher.enqueue(1, True, state, "good")
        await asyncio.wait_for(state.queue.join(), timeout=1)

        assert processed, ["bad", "good"]
        await dispatcher.shutdown()
