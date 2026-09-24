"""Keep the authenticated gateway allowlist aligned with the auth MCP contract."""

import re
from pathlib import Path

from fastmcp import Client

from avanza_mcp.auth.browser import BrowserAuth
from avanza_mcp.auth.server import create_auth_server


async def test_authenticated_gateway_allowlist_matches_auth_server_tool_surface():
    policy = Path("public/gateway/policy.mjs").read_text(encoding="utf-8")

    public_match = re.search(
        r"export const DEFAULT_ALLOWED_TOOLS = \[(.*?)\]\.join\(','\);",
        policy,
        flags=re.DOTALL,
    )
    extra_match = re.search(
        r"export const AUTHENTICATED_EXTRA_TOOLS = \[(.*?)\]\.join\(','\);",
        policy,
        flags=re.DOTALL,
    )
    assert public_match, "DEFAULT_ALLOWED_TOOLS block not found"
    assert extra_match, "AUTHENTICATED_EXTRA_TOOLS block not found"

    public_tools = set(re.findall(r"'([a-z][a-z0-9_]*)'", public_match.group(1)))
    extra_tools = set(re.findall(r"'([a-z][a-z0-9_]*)'", extra_match.group(1)))
    allowed_tools = public_tools | extra_tools

    async with Client(create_auth_server(BrowserAuth(store=None))) as client:
        server_tools = {tool.name for tool in await client.list_tools()}

    assert len(public_tools) == 37
    assert len(extra_tools) == 14
    assert len(allowed_tools) == 51
    assert allowed_tools == server_tools
    assert {"get_credit_info", "get_current_offers", "get_forum_posts"}.isdisjoint(
        allowed_tools
    )
