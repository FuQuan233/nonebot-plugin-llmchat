import asyncio
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from nonebot import logger

from .config import MCPServerConfig
from .onebottools import OneBotTools


class MCPClient:
    _instance = None
    _initialized = False

    def __new__(cls, server_config: dict[str, MCPServerConfig] | None = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, server_config: dict[str, MCPServerConfig] | None = None):
        if self._initialized:
            return

        if server_config is None:
            raise ValueError("server_config must be provided for first initialization")

        logger.info(f"正在初始化MCPClient单例，共有{len(server_config)}个服务器配置")
        self.server_config = server_config
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        # 添加工具列表缓存
        self._tools_cache: list | None = None
        self._cache_initialized = False
        # 初始化OneBot工具
        self.onebot_tools = OneBotTools()
        self._initialized = True
        logger.debug("MCPClient单例初始化成功")

    @classmethod
    def get_instance(cls, server_config: dict[str, MCPServerConfig] | None = None):
        """获取MCPClient实例"""
        if cls._instance is None:
            if server_config is None:
                raise ValueError("server_config must be provided for first initialization")
            cls._instance = cls(server_config)
        return cls._instance

    @classmethod
    def instance(cls):
        """快速获取已初始化的MCPClient实例，如果未初始化则抛出异常"""
        if cls._instance is None:
            raise RuntimeError("MCPClient has not been initialized. Call get_instance() first.")
        return cls._instance

    async def connect_to_servers(self):
        logger.info(f"开始连接{len(self.server_config)}个MCP服务器")
        for server_name, config in self.server_config.items():
            logger.debug(f"正在连接服务器[{server_name}]")
            if config.url:
                sse_transport = await self.exit_stack.enter_async_context(sse_client(url=config.url, headers=config.headers))
                read, write = sse_transport
                self.sessions[server_name] = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await self.sessions[server_name].initialize()
            elif config.command:
                stdio_transport = await self.exit_stack.enter_async_context(
                    stdio_client(StdioServerParameters(**config.model_dump()))
                )
                read, write = stdio_transport
                self.sessions[server_name] = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await self.sessions[server_name].initialize()
            else:
                raise ValueError("Server config must have either url or command")

            logger.info(f"已成功连接到MCP服务器[{server_name}]")

    def _create_session_context(self, server_name: str):
        """创建临时会话的异步上下文管理器"""
        config = self.server_config[server_name]

        class SessionContext:
            def __init__(self):
                self.session = None
                self.exit_stack = AsyncExitStack()

            async def __aenter__(self):
                if config.url:
                    transport = await self.exit_stack.enter_async_context(
                        sse_client(url=config.url, headers=config.headers)
                    )
                elif config.command:
                    transport = await self.exit_stack.enter_async_context(
                        stdio_client(StdioServerParameters(**config.model_dump()))
                    )
                else:
                    raise ValueError("Server config must have either url or command")

                read, write = transport
                self.session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                await self.session.initialize()
                return self.session

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                await self.exit_stack.aclose()

        return SessionContext()

    async def get_available_tools(self):
        """获取可用工具列表，使用缓存机制"""
        if self._tools_cache is not None:
            logger.debug("返回缓存的工具列表")
            return self._tools_cache

        logger.info(f"初始化工具列表缓存，需要连接{len(self.server_config)}个服务器")
        available_tools = []

        # 添加OneBot内置工具
        onebot_tools = self.onebot_tools.get_available_tools()
        available_tools.extend(onebot_tools)
        logger.debug(f"添加了{len(onebot_tools)}个OneBot内置工具")

        # 添加MCP服务器工具
        for server_name in self.server_config.keys():
            logger.debug(f"正在从服务器[{server_name}]获取工具列表")
            async with self._create_session_context(server_name) as session:
                response = await session.list_tools()
                tools = response.tools
                logger.debug(f"在服务器[{server_name}]中找到{len(tools)}个工具")

                available_tools.extend(
                    {
                        "type": "function",
                        "function": {
                            "name": f"{server_name}___{tool.name}",
                            "description": tool.description,
                            "parameters": tool.inputSchema,
                        },
                    }
                    for tool in tools
                )

        # 缓存工具列表
        self._tools_cache = available_tools
        self._cache_initialized = True
        logger.info(f"工具列表缓存完成，共缓存{len(available_tools)}个工具")
        return available_tools

    async def call_tool(self, tool_name: str, tool_args: dict, group_id: int | None = None, bot_id: str | None = None):
        """按需连接调用工具，调用后立即断开"""
        # 检查是否是QQ工具
        if tool_name.startswith("ob__"):
            if group_id is None or bot_id is None:
                return "QQ工具需要提供group_id和bot_id参数"
            logger.info(f"调用OneBot工具[{tool_name}]")
            return await self.onebot_tools.call_tool(tool_name, tool_args, group_id, bot_id)

        # MCP工具处理
        server_name, real_tool_name = tool_name.split("___")
        logger.info(f"按需连接到服务器[{server_name}]调用工具[{real_tool_name}]")

        async with self._create_session_context(server_name) as session:
            try:
                response = await asyncio.wait_for(session.call_tool(real_tool_name, tool_args), timeout=30)
                logger.debug(f"工具[{real_tool_name}]调用完成，响应: {response}")
                return response.content
            except asyncio.TimeoutError:
                logger.error(f"调用工具[{real_tool_name}]超时")
                return f"调用工具[{real_tool_name}]超时"

    def get_friendly_name(self, tool_name: str):
        logger.debug(tool_name)
        # 检查是否是OneBot工具
        if tool_name.startswith("ob__"):
            return self.onebot_tools.get_friendly_name(tool_name)
        
        # MCP工具处理
        server_name, real_tool_name = tool_name.split("___")
        return (self.server_config[server_name].friendly_name or server_name) + " - " + real_tool_name

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
