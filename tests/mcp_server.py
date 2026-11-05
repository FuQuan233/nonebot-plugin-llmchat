"""供 MCP 传输回归测试使用的真实 SDK 服务器，也可作为 stdio 子进程启动。"""

from mcp import MCPError
from mcp.server import MCPServer
from mcp.types import REQUEST_TIMEOUT


def make_server() -> MCPServer:
    server = MCPServer("llmchat-regression")

    @server.tool()
    async def echo(text: str) -> str:
        """Return the supplied text."""
        return text

    @server.tool()
    async def protocol_error() -> str:
        """Return a request error without disconnecting the client."""
        raise MCPError(-32602, "Invalid test arguments")

    @server.tool()
    async def protocol_timeout() -> str:
        """Return the v2 request-timeout error code."""
        raise MCPError(REQUEST_TIMEOUT, "Test request timed out")

    return server


if __name__ == "__main__":
    make_server().run()
