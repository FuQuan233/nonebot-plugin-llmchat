import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from time import monotonic
from typing import cast

import httpx2
from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT
from nonebot import logger

from .config import MCPServerConfig
from .onebottools import OneBotTools


@dataclass(slots=True)
class _SessionHandle:
    """让创建会话的 Task 负责关闭该会话。"""

    stop_event: asyncio.Event
    owner_task: asyncio.Task[None]


class MCPClient:
    _instance = None
    _initialized = False
    _SESSION_TTL_SECONDS = 600
    _SESSION_CLEANUP_INTERVAL_SECONDS = 60

    def __new__(
        cls,
        server_config: dict[str, MCPServerConfig] | None = None,
        default_command_cwd: str | None = None,
        operation_timeout: float = 30,
    ):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        server_config: dict[str, MCPServerConfig] | None = None,
        default_command_cwd: str | None = None,
        operation_timeout: float = 100,
    ):
        if self._initialized:
            return

        if server_config is None:
            raise ValueError("server_config must be provided for first initialization")

        logger.info(f"正在初始化MCPClient单例，共有{len(server_config)}个服务器配置")
        self.server_config = server_config
        self.default_command_cwd = default_command_cwd
        self.operation_timeout = operation_timeout
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self._session_handles: dict[str, _SessionHandle] = {}
        self._session_last_used: dict[str, float] = {}
        self._session_lock = asyncio.Lock()
        self._session_cleanup_task: asyncio.Task | None = None
        # 添加工具列表缓存
        self._tools_cache: list | None = None
        self._cache_initialized = False
        # 初始化OneBot工具
        self.onebot_tools = OneBotTools()
        self._initialized = True
        logger.debug("MCPClient单例初始化成功")

    @classmethod
    def get_instance(
        cls,
        server_config: dict[str, MCPServerConfig] | None = None,
        default_command_cwd: str | None = None,
        operation_timeout: float = 30,
    ):
        """获取MCPClient实例"""
        if cls._instance is None:
            if server_config is None:
                raise ValueError("server_config must be provided for first initialization")
            cls._instance = cls(server_config, default_command_cwd, operation_timeout)
        return cls._instance

    @classmethod
    def instance(cls):
        """快速获取已初始化的MCPClient实例，如果未初始化则抛出异常"""
        if cls._instance is None:
            raise RuntimeError("MCPClient has not been initialized. Call get_instance() first.")
        return cls._instance

    async def connect_to_servers(self):
        await self._ensure_cleanup_task()
        logger.info(f"开始连接{len(self.server_config)}个MCP服务器")
        for server_name in self.server_config:
            logger.debug(f"正在连接服务器[{server_name}]")
            await self._get_or_create_session(server_name)
            logger.info(f"已成功连接到MCP服务器[{server_name}]")

    async def _run_server_session(
        self,
        server_name: str,
        ready: asyncio.Future[ClientSession],
        stop_event: asyncio.Event,
    ) -> None:
        """在同一个 Task 中创建并销毁会话。

        MCP 的传输层使用 AnyIO cancel scope，异步上下文必须由进入它的
        Task 退出，因此不能把 AsyncExitStack 返回给调用方再关闭。
        """
        session_stack = AsyncExitStack()
        try:
            session, _ = await self._initialize_server_session(server_name, session_stack)
            ready.set_result(session)
            await stop_event.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
        finally:
            try:
                await session_stack.aclose()
            except Exception as error:
                logger.opt(exception=error).error(f"关闭MCP会话[{server_name}]失败")

    async def _initialize_server_session(
        self,
        server_name: str,
        session_stack: AsyncExitStack,
    ) -> tuple[ClientSession, AsyncExitStack]:
        """创建会话，由 SDK 协商协议版本并兼容旧版 initialize 握手。"""
        config = self.server_config[server_name]
        if config.url:
            if config.transport:
                session = await self._connect_http_session(config, config.transport, session_stack)
            else:
                try:
                    session = await self._connect_http_session(config, "streamable_http", session_stack)
                except Exception as error:
                    logger.debug(f"服务器[{server_name}]Streamable HTTP 探测失败({error})，回退到 SSE")
                    session = await self._connect_http_session(config, "sse", session_stack)
        elif config.command:
            params = StdioServerParameters(
                command=config.command,
                args=config.args or [],
                env=config.env or {},
                cwd=self.default_command_cwd,
            )
            client = await session_stack.enter_async_context(Client(params, read_timeout_seconds=self.operation_timeout))
            session = client.session
        else:
            raise ValueError("Server config must have either url or command")
        return session, session_stack

    async def _connect_http_session(
        self,
        config: MCPServerConfig,
        transport_type: str,
        session_stack: AsyncExitStack,
    ) -> ClientSession:
        """探测失败时先释放全部资源，成功后交给会话所有者统一关闭。"""
        async with AsyncExitStack() as attempt_stack:
            if transport_type == "streamable_http":
                http_client = await attempt_stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers=config.headers or {},
                        timeout=httpx2.Timeout(self.operation_timeout, read=max(300, self.operation_timeout)),
                    )
                )
                transport = streamable_http_client(url=cast(str, config.url), http_client=http_client)
            else:
                transport = sse_client(
                    url=cast(str, config.url),
                    headers=config.headers,
                    timeout=self.operation_timeout,
                    sse_read_timeout=max(300, self.operation_timeout),
                )
            client = await attempt_stack.enter_async_context(Client(transport, read_timeout_seconds=self.operation_timeout))
            session_stack.push_async_exit(attempt_stack.pop_all())
            return client.session

    async def _create_server_session(self, server_name: str) -> tuple[ClientSession, _SessionHandle]:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[ClientSession] = loop.create_future()
        stop_event = asyncio.Event()
        owner_task = asyncio.create_task(
            self._run_server_session(server_name, ready, stop_event),
            name=f"llmchat-mcp-{server_name}",
        )
        try:
            session = await asyncio.wait_for(asyncio.shield(ready), timeout=self.operation_timeout)
        except TimeoutError as error:
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)
            raise TimeoutError(f"连接MCP服务器[{server_name}]超时") from error
        except BaseException:
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)
            raise
        return session, _SessionHandle(stop_event=stop_event, owner_task=owner_task)

    async def _close_server_session(self, server_name: str):
        """关闭指定服务器会话。"""
        handle = self._session_handles.pop(server_name, None)
        self.sessions.pop(server_name, None)
        self._session_last_used.pop(server_name, None)

        if handle is not None:
            handle.stop_event.set()
            try:
                await asyncio.wait_for(
                    asyncio.gather(handle.owner_task, return_exceptions=True),
                    timeout=self.operation_timeout + 1,
                )
            except TimeoutError:
                logger.error(f"等待MCP会话[{server_name}]关闭超时")

    async def _get_or_create_session(self, server_name: str) -> ClientSession:
        """获取可复用会话；若不存在或已过期则新建。"""
        now = monotonic()
        async with self._session_lock:
            last_used = self._session_last_used.get(server_name)
            session = self.sessions.get(server_name)
            handle = self._session_handles.get(server_name)

            if session is not None and handle is not None and handle.owner_task.done():
                await self._close_server_session(server_name)
                session = None

            # 空闲超过阈值则销毁重建
            if session is not None and last_used is not None:
                if now - last_used > self._SESSION_TTL_SECONDS:
                    logger.info(f"服务器[{server_name}]会话空闲超过10分钟，重新创建")
                    await self._close_server_session(server_name)
                    session = None

            if session is None:
                session, handle = await self._create_server_session(server_name)
                self.sessions[server_name] = session
                self._session_handles[server_name] = handle

            self._session_last_used[server_name] = now
            return self.sessions[server_name]

    async def _cleanup_expired_sessions(self):
        """回收空闲过期会话。"""
        now = monotonic()
        async with self._session_lock:
            expired_servers = [
                server_name
                for server_name, last_used in self._session_last_used.items()
                if now - last_used > self._SESSION_TTL_SECONDS
            ]

            for server_name in expired_servers:
                logger.info(f"回收空闲MCP会话[{server_name}]")
                await self._close_server_session(server_name)

    async def _session_cleanup_loop(self):
        try:
            while True:
                await asyncio.sleep(self._SESSION_CLEANUP_INTERVAL_SECONDS)
                await self._cleanup_expired_sessions()
        except asyncio.CancelledError:
            logger.debug("MCP会话清理任务已取消")
            raise

    async def _ensure_cleanup_task(self):
        if self._session_cleanup_task is None or self._session_cleanup_task.done():
            self._session_cleanup_task = asyncio.create_task(self._session_cleanup_loop())

    async def init_tools_cache(self):
        """初始化工具列表缓存"""
        if not self._cache_initialized:
            await self._ensure_cleanup_task()
            available_tools = []
            logger.info(f"初始化工具列表缓存，需要连接{len(self.server_config)}个服务器")
            for server_name in self.server_config.keys():
                logger.debug(f"正在从服务器[{server_name}]获取工具列表")
                session = await self._get_or_create_session(server_name)
                response = await asyncio.wait_for(session.list_tools(), timeout=self.operation_timeout)
                tools = response.tools
                logger.debug(f"在服务器[{server_name}]中找到{len(tools)}个工具")

                available_tools.extend(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{server_name}__{tool.name}",
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        },
                    }
                    for tool in tools
                )

            # 缓存工具列表
            self._tools_cache = available_tools
            self._cache_initialized = True

            logger.info(f"工具列表缓存完成，共缓存{len(available_tools)}个工具")

    async def get_available_tools(self, is_group: bool):
        """获取可用工具列表，使用缓存机制"""
        await self.init_tools_cache()
        available_tools = self._tools_cache.copy() if self._tools_cache else []
        if is_group:
            # 群聊场景，包含OneBot工具和MCP工具
            available_tools.extend(self.onebot_tools.get_available_tools())
        logger.debug(f"获取可用工具列表，共{len(available_tools)}个工具")
        return available_tools

    async def call_tool(self, tool_name: str, tool_args: dict, group_id: int | None = None, bot_id: str | None = None):
        """按需调用工具，MCP会话会在10分钟空闲后自动回收。"""
        # 检查是否是OneBot内置工具
        if tool_name.startswith("ob__"):
            if group_id is None or bot_id is None:
                return "QQ工具需要提供group_id和bot_id参数"
            logger.info(f"调用OneBot工具[{tool_name}]")
            try:
                return await asyncio.wait_for(
                    self.onebot_tools.call_tool(tool_name, tool_args, group_id, bot_id),
                    timeout=self.operation_timeout,
                )
            except asyncio.TimeoutError:
                return f"调用OneBot工具[{tool_name}]超时"

        # 检查是否是MCP工具
        if tool_name.startswith("mcp__"):
            # MCP工具处理：mcp__server_name__tool_name
            parts = tool_name.split("__")
            if len(parts) != 3 or parts[0] != "mcp":
                return f"MCP工具名称格式错误: {tool_name}"

            server_name = parts[1]
            real_tool_name = parts[2]
            if server_name not in self.server_config:
                return f"未知的MCP服务器: {server_name}"
            logger.info(f"按需连接到服务器[{server_name}]调用工具[{real_tool_name}]")

            try:
                await self._ensure_cleanup_task()
                session = await self._get_or_create_session(server_name)
                response = await asyncio.wait_for(session.call_tool(real_tool_name, tool_args), timeout=self.operation_timeout)
                logger.debug(f"工具[{real_tool_name}]调用完成，响应: {response}")
                return response.content
            except asyncio.TimeoutError:
                logger.error(f"调用工具[{real_tool_name}]超时")
                return f"调用工具[{real_tool_name}]超时"
            except MCPError as error:
                # v2 将 HTTP 错误映射为单次请求的协议错误，会话仍可继续使用。
                if error.code == REQUEST_TIMEOUT:
                    return f"调用工具[{real_tool_name}]超时"
                logger.warning(f"调用工具[{real_tool_name}]失败: {error}")
                return f"调用工具[{real_tool_name}]失败: {error}"
            except Exception as e:
                logger.opt(exception=e).error(f"调用工具[{real_tool_name}]失败，准备重置会话")
                async with self._session_lock:
                    await self._close_server_session(server_name)
                return f"调用工具[{real_tool_name}]失败: {e!s}"

        # 未知工具类型
        return f"未知的工具类型: {tool_name}"

    def get_friendly_name(self, tool_name: str):
        logger.debug(tool_name)
        # 检查是否是OneBot内置工具
        if tool_name.startswith("ob__"):
            return self.onebot_tools.get_friendly_name(tool_name)

        # 检查是否是MCP工具
        if tool_name.startswith("mcp__"):
            # MCP工具处理：mcp__server_name__tool_name
            parts = tool_name.split("__")
            if len(parts) != 3 or parts[0] != "mcp":
                return tool_name  # 格式错误时返回原名称

            server_name = parts[1]
            real_tool_name = parts[2]
            server = self.server_config.get(server_name)
            return ((server.friendly_name if server else None) or server_name) + " - " + real_tool_name

        # 未知工具类型，返回原名称
        return tool_name

    def clear_tools_cache(self):
        """清除工具列表缓存"""
        logger.info("清除工具列表缓存")
        self._tools_cache = None
        self._cache_initialized = False

    async def cleanup(self):
        """清理资源（不销毁单例）"""
        logger.debug("正在清理MCPClient资源")
        # 只清除缓存，不销毁单例
        # self.clear_tools_cache()  # 保留缓存，避免重复获取工具列表

        if self._session_cleanup_task is not None:
            self._session_cleanup_task.cancel()
            try:
                await self._session_cleanup_task
            except asyncio.CancelledError:
                pass
            self._session_cleanup_task = None

        async with self._session_lock:
            for server_name in list(self.sessions.keys()):
                await self._close_server_session(server_name)

        await self.exit_stack.aclose()
        # 重新初始化exit_stack以便后续使用
        self.exit_stack = AsyncExitStack()
        logger.debug("MCPClient资源清理完成")

    @classmethod
    async def destroy_instance(cls):
        """完全销毁单例实例（仅在应用关闭时使用）"""
        if cls._instance is not None:
            logger.info("销毁MCPClient单例")
            await cls._instance.cleanup()
            cls._instance.clear_tools_cache()
            cls._instance = None
            cls._initialized = False
            logger.debug("MCPClient单例已销毁")
