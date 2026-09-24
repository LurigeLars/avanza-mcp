"""Offline transport check for the canonical authenticated HTTP server."""

from fastmcp import Client
from fastmcp.utilities.tests import run_server_async

from avanza_mcp.auth.browser import BrowserAuth
from avanza_mcp.auth.server import create_auth_server


async def test_authenticated_http_discovery_uses_single_combined_avanza_surface():
    server = create_auth_server(BrowserAuth(store=None))

    async with run_server_async(server) as url:
        async with Client(url) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert len(tools) == 51
            assert "get_stock_quote" in tools
            assert "connect_avanza" in tools
            assert "get_accounts" in tools
            assert "get_active_orders" in tools
            assert "get_credit_info" not in tools
            assert "get_current_offers" not in tools
            assert "get_forum_posts" not in tools

            status = await client.call_tool("get_auth_status", {})
            assert status.structured_content["state"] == "disconnected"
