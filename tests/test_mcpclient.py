import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from anyio import CancelScope

from nonebot_plugin_llmchat.mcpclient import MCPClient


class TestMCPClientSessionOwnership(IsolatedAsyncioTestCase):
    async def test_session_context_is_closed_by_its_owner_task(self):
        entered_by = None
        exited_by = None

        @asynccontextmanager
        async def task_bound_context():
            nonlocal entered_by, exited_by
            entered_by = asyncio.current_task()
            with CancelScope():
                try:
                    yield SimpleNamespace()
                finally:
                    exited_by = asyncio.current_task()

        client = object.__new__(MCPClient)
        client.operation_timeout = 1

        async def initialize(server_name, session_stack):
            session = await session_stack.enter_async_context(task_bound_context())
            return session, session_stack

        client._initialize_server_session = initialize

        session, handle = await client._create_server_session("test")
        client.sessions = {"test": session}
        client._session_handles = {"test": handle}
        client._session_last_used = {"test": 0.0}

        await client._close_server_session("test")

        assert entered_by is handle.owner_task
        assert exited_by is handle.owner_task
        assert handle.owner_task.done()
