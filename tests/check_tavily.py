"""手动运行 Tavily 官方免密端点冒烟检查，不加入离线回归测试。"""

# ruff: noqa: I001
import argparse
import asyncio
import json
import os

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import MCPServerConfig
from nonebot_plugin_llmchat.mcpclient import MCPClient


async def main(*, list_only: bool = False) -> None:
    api_key = os.environ.get("TAVILY_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {"X-Tavily-Access-Mode": "keyless"}
    for transport in (None, "streamable_http"):
        client = MCPClient.get_instance(
            {
                "tavily": MCPServerConfig(
                    url="https://mcp.tavily.com/mcp/",
                    headers=headers,
                    transport=transport,
                )
            },
            operation_timeout=30,
        )
        try:
            tools = await client.get_available_tools(is_group=False)
            search = next(tool["function"] for tool in tools if "search" in tool["function"]["name"])
            if transport is not None and not list_only:
                result = await client.call_tool(
                    search["name"],
                    {"query": "site:modelcontextprotocol.io Python SDK", "max_results": 1},
                )
                if not isinstance(result, list):
                    raise RuntimeError(f"Tavily search failed: {result}")
                payload = json.loads(next(block.text for block in result if block.type == "text"))
                if not payload.get("results"):
                    raise RuntimeError(f"Tavily search unavailable: {payload.get('code', 'no_results')}")
            print(  # noqa: T201
                f"Tavily {transport or 'auto'}: {len(tools)} tools, "
                f"protocol={client.sessions['tavily'].protocol_version}, "
                f"search={'passed' if transport and not list_only else 'not requested'}"
            )
        finally:
            await MCPClient.destroy_instance()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-only", action="store_true", help="Only check connections and tool discovery; do not search")
    asyncio.run(main(list_only=parser.parse_args().list_only))
