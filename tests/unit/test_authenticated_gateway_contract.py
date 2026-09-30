"""Keep the single gateway allowlist aligned with the authenticated MCP contract."""

import re
from pathlib import Path

from fastmcp import Client

from avanza_mcp.auth.broker import AuthProcessBroker
from avanza_mcp.auth.server import create_auth_server


async def test_gateway_allowlist_matches_single_authenticated_server_surface():
    policy = Path("public/gateway/policy.mjs").read_text(encoding="utf-8")

    market_match = re.search(
        r"const MARKET_TOOLS = \[(.*?)\]\.join\(','\);",
        policy,
        flags=re.DOTALL,
    )
    auth_match = re.search(
        r"const AUTH_TOOLS = \[(.*?)\]\.join\(','\);",
        policy,
        flags=re.DOTALL,
    )
    assert market_match, "MARKET_TOOLS block not found"
    assert auth_match, "AUTH_TOOLS block not found"

    market_tools = set(re.findall(r"'([a-z][a-z0-9_]*)'", market_match.group(1)))
    auth_tools = set(re.findall(r"'([a-z][a-z0-9_]*)'", auth_match.group(1)))
    allowed_tools = market_tools | auth_tools

    async with Client(create_auth_server(AuthProcessBroker(mode="one_shot"))) as client:
        server_tools = {tool.name for tool in await client.list_tools()}

    assert len(market_tools) == 37
    assert len(auth_tools) == 17
    assert len(allowed_tools) == 54
    assert allowed_tools == server_tools
    assert "enrich_leveraged_snapshot" not in allowed_tools
    assert {"get_credit_info", "get_current_offers", "get_forum_posts"}.isdisjoint(
        allowed_tools
    )
