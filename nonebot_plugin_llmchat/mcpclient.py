from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from nonebot import logger

from .config import MCPServerConfig


class MCPClient:
    def __init__(self, server_config: dict[str, MCPServerConfig]):
        logger.info(f"正在初始化MCPClient，共有{len(server_config)}个服务器配置")
        self.server_config = server_config
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        logger.debug("MCPClient初始化成功")

    async def connect_to_servers(self):
        logger.info(f"开始连接{len(self.server_config)}个MCP服务器")
        for server_name, config in self.server_config.items():
            logger.debug(f"正在连接服务器[{server_name}]")
            if config.url:
                sse_transport = await self.exit_stack.enter_async_context(sse_client(url=config.url))
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

    async def get_available_tools(self):
        logger.info(f"正在从{len(self.sessions)}个已连接的服务器获取可用工具")
        available_tools = []

        for server_name, session in self.sessions.items():
            logger.debug(f"正在列出服务器[{server_name}]中的工具")
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
        return available_tools

    async def call_tool(self, tool_name: str, tool_args: dict):
        server_name, real_tool_name = tool_name.split("___")
        logger.info(f"正在服务器[{server_name}]上调用工具[{real_tool_name}]")
        session = self.sessions[server_name]
        response = await session.call_tool(real_tool_name, tool_args)
        logger.debug(f"工具[{real_tool_name}]调用完成，响应: {response}")
        return response

    def get_friendly_name(self, tool_name: str):
        server_name, real_tool_name = tool_name.split("___")
        return (self.server_config[server_name].friendly_name or server_name) + " - " + real_tool_name

    async def cleanup(self):
        logger.debug("正在清理MCPClient资源")
        await self.exit_stack.aclose()
        logger.debug("MCPClient资源清理完成")
