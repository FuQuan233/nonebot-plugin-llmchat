# ruff: noqa: I001
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from anyio import CancelScope
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn

import tests.bootstrap  # noqa: F401
from tests.mcp_server import make_server
from nonebot_plugin_llmchat.config import MCPServerConfig
from nonebot_plugin_llmchat.mcpclient import MCPClient


class RecordingServer:
    """记录真实 HTTP 请求，可模拟只支持旧版握手的远程服务。"""

    def __init__(self, app: ASGIApp, *, legacy: bool = False, authorization: str | None = None):
        self.app = app
        self.legacy = legacy
        self.authorization = authorization
        self.fail_next_call = False
        self.delay_discover = 0.0
        self.requests: list[dict] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = b""
        if scope["method"] == "POST":
            while True:
                event = await receive()
                body += event.get("body", b"")
                if not event.get("more_body", False):
                    break
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}
        message = json.loads(body) if body else {}
        self.requests.append(
            {"method": scope["method"], "path": scope["path"], "query": scope["query_string"], "headers": headers, "rpc": message}
        )
        if self.authorization is not None and headers.get("authorization") != self.authorization:
            await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
            return
        if message.get("method") == "server/discover":
            await asyncio.sleep(self.delay_discover)
        if self.legacy and message.get("method") == "server/discover":
            await JSONResponse({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}})(
                scope, receive, send
            )
            return

        if self.fail_next_call and message.get("method") == "tools/call":
            self.fail_next_call = False
            await JSONResponse(
                {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": "Temporary server failure"}},
                status_code=500,
            )(scope, receive, send)
            return

        replayed = False

        async def replay_body():
            nonlocal replayed
            if not replayed and scope["method"] == "POST":
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_body, send)


@asynccontextmanager
async def running_server(app: ASGIApp):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", ws="none", timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve(sockets=[listener]))

    async def wait_for_startup():
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("MCP test server stopped before startup")
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_startup(), timeout=5)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            listener.close()


@asynccontextmanager
async def plugin_client(config: MCPServerConfig):
    # 避免测试之间共享插件的进程单例。
    client = object.__new__(MCPClient)
    client.__init__({"test": config}, operation_timeout=5)
    try:
        yield client
    finally:
        await client.cleanup()


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


class TestMCPTransports(IsolatedAsyncioTestCase):
    async def assert_echo(self, client: MCPClient):
        tools = await client.get_available_tools(is_group=False)
        echo = next(tool["function"] for tool in tools if tool["function"]["name"] == "mcp__test__echo")
        assert echo["parameters"]["properties"]["text"]["type"] == "string"
        result = await client.call_tool("mcp__test__echo", {"text": "你好 MCP v2"})
        assert isinstance(result, list)
        content = result[0]
        assert content.type == "text"
        assert content.text == "你好 MCP v2"

    async def test_streamable_http_handles_json_and_sse_responses(self):
        for json_response in (False, True):
            with self.subTest(json_response=json_response):
                app = RecordingServer(make_server().streamable_http_app(json_response=json_response))
                async with running_server(app) as url:
                    async with plugin_client(MCPServerConfig(url=f"{url}/mcp", transport="streamable_http")) as client:
                        await self.assert_echo(client)
                        assert client.sessions["test"].protocol_version == "2026-07-28"

    async def test_auto_transport_prefers_streamable_http(self):
        app = RecordingServer(make_server().streamable_http_app())
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/mcp")) as client:
                await self.assert_echo(client)
        assert app.requests[0]["method"] == "POST"
        assert all(request["method"] != "GET" for request in app.requests)

    async def test_tavily_style_stateless_server_preserves_query_and_headers(self):
        for transport in (None, "streamable_http"):
            with self.subTest(transport=transport):
                app = RecordingServer(
                    make_server().streamable_http_app(stateless_http=True, json_response=True, streamable_http_path="/mcp/"),
                    legacy=True,
                    authorization="Bearer regression-test",
                )
                async with running_server(app) as url:
                    config = MCPServerConfig(
                        url=f"{url}/mcp/?tavilyApiKey=regression-test",
                        headers={"Authorization": "Bearer regression-test", "X-Test-Client": "llmchat"},
                        transport=transport,
                    )
                    async with plugin_client(config) as client:
                        await self.assert_echo(client)
                        assert client.sessions["test"].protocol_version == "2025-11-25"
                methods = [request["rpc"].get("method") for request in app.requests]
                assert "initialize" in methods
                assert "notifications/initialized" in methods
                for request in app.requests:
                    assert request["query"] == b"tavilyApiKey=regression-test"
                    assert request["headers"]["authorization"] == "Bearer regression-test"
                    assert request["headers"]["x-test-client"] == "llmchat"

    async def test_stateful_legacy_server_terminates_session_on_cleanup(self):
        app = RecordingServer(make_server().streamable_http_app(), legacy=True, authorization="Bearer regression-test")
        async with running_server(app) as url:
            config = MCPServerConfig(url=f"{url}/mcp", headers={"Authorization": "Bearer regression-test"})
            async with plugin_client(config) as client:
                await self.assert_echo(client)
                owner = client._session_handles["test"].owner_task
            assert owner.done()
            assert not client.sessions
            assert not client._session_handles
        deletes = [request for request in app.requests if request["method"] == "DELETE"]
        assert len(deletes) == 1
        assert deletes[0]["headers"]["mcp-session-id"]
        assert deletes[0]["headers"]["authorization"] == "Bearer regression-test"

    async def test_auto_transport_falls_back_to_sse_after_failed_http_handshake(self):
        app = RecordingServer(make_server().sse_app())
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/sse")) as client:
                await self.assert_echo(client)
        assert app.requests[0]["method"] == "POST"
        assert any(request["method"] == "GET" and request["path"] == "/sse" for request in app.requests)

    async def test_explicit_sse_uses_the_sse_transport(self):
        app = RecordingServer(make_server().sse_app(), authorization="Bearer regression-test")
        async with running_server(app) as url:
            config = MCPServerConfig(url=f"{url}/sse", transport="sse", headers={"Authorization": "Bearer regression-test"})
            async with plugin_client(config) as client:
                await self.assert_echo(client)
        assert app.requests[0]["method"] == "GET"

    async def test_explicit_http_does_not_fall_back_to_sse(self):
        app = RecordingServer(make_server().sse_app())
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/sse", transport="streamable_http")) as client:
                result = await client.call_tool("mcp__test__echo", {"text": "wrong transport"})
                assert isinstance(result, str)
                assert "失败" in result
                assert not client.sessions
        assert all(request["method"] == "POST" for request in app.requests)

    async def test_stdio_uses_the_project_interpreter(self):
        config = MCPServerConfig(command=sys.executable, args=[str(Path(__file__).with_name("mcp_server.py"))])
        async with plugin_client(config) as client:
            await self.assert_echo(client)
            assert client.sessions["test"].protocol_version == "2026-07-28"

    async def test_protocol_errors_and_timeouts_keep_the_session_usable(self):
        app = RecordingServer(make_server().streamable_http_app())
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/mcp")) as client:
                await self.assert_echo(client)
                session = client.sessions["test"]
                error = await client.call_tool("mcp__test__protocol_error", {})
                assert "Invalid test arguments" in error
                timeout = await client.call_tool("mcp__test__protocol_timeout", {})
                assert "超时" in timeout
                await self.assert_echo(client)
                assert client.sessions["test"] is session

    async def test_finished_owner_task_is_reconnected(self):
        app = RecordingServer(make_server().streamable_http_app())
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/mcp")) as client:
                await self.assert_echo(client)
                session = client.sessions["test"]
                handle = client._session_handles["test"]
                handle.stop_event.set()
                await asyncio.wait_for(handle.owner_task, timeout=5)
                await self.assert_echo(client)
                assert client.sessions["test"] is not session

    async def test_http_error_only_fails_one_request(self):
        app = RecordingServer(make_server().streamable_http_app())
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/mcp")) as client:
                await self.assert_echo(client)
                session = client.sessions["test"]
                app.fail_next_call = True
                result = await client.call_tool("mcp__test__echo", {"text": "fail once"})
                assert "Temporary server failure" in result
                await self.assert_echo(client)
                assert client.sessions["test"] is session

    async def test_cancelled_owner_task_is_reconnected(self):
        app = RecordingServer(make_server().streamable_http_app())
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/mcp")) as client:
                await self.assert_echo(client)
                session = client.sessions["test"]
                handle = client._session_handles["test"]
                handle.owner_task.cancel()
                await asyncio.gather(handle.owner_task, return_exceptions=True)
                await self.assert_echo(client)
                assert client.sessions["test"] is not session

    async def test_same_origin_redirect_preserves_post_and_credentials(self):
        app = RecordingServer(
            make_server().streamable_http_app(streamable_http_path="/mcp/"), authorization="Bearer regression-test"
        )
        async with running_server(app) as url:
            config = MCPServerConfig(
                url=f"{url}/mcp?tavilyApiKey=regression-test",
                transport="streamable_http",
                headers={"Authorization": "Bearer regression-test"},
            )
            async with plugin_client(config) as client:
                await self.assert_echo(client)
        assert any(request["path"] == "/mcp/" for request in app.requests)
        for request in app.requests:
            assert request["method"] == "POST"
            assert request["headers"]["authorization"] == "Bearer regression-test"
            assert request["query"] == b"tavilyApiKey=regression-test"

    async def test_connection_timeout_closes_owner_and_allows_reconnection(self):
        app = RecordingServer(make_server().streamable_http_app())
        app.delay_discover = 1.0
        async with running_server(app) as url:
            async with plugin_client(MCPServerConfig(url=f"{url}/mcp", transport="streamable_http")) as client:
                client.operation_timeout = 0.1
                result = await client.call_tool("mcp__test__echo", {"text": "timed out"})
                assert "超时" in result
                assert not client.sessions
                assert not any(task.get_name() == "llmchat-mcp-test" for task in asyncio.all_tasks())
                app.delay_discover = 0.0
                client.operation_timeout = 5
                await self.assert_echo(client)
