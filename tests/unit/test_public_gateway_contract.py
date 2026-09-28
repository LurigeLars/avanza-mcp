"""Keep internal market tools aligned with the single gateway allowlist."""

import re
from pathlib import Path

from fastmcp import Client

from avanza_mcp import mcp


async def test_market_tool_block_matches_internal_market_surface():
    policy = Path("public/gateway/policy.mjs").read_text(encoding="utf-8")
    match = re.search(
        r"const MARKET_TOOLS = \[(.*?)\]\.join\(','\);",
        policy,
        flags=re.DOTALL,
    )
    assert match, "MARKET_TOOLS block not found"
    market_tools = set(re.findall(r"'([a-z][a-z0-9_]*)'", match.group(1)))

    async with Client(mcp) as client:
        server_tools = {tool.name for tool in await client.list_tools()}

    assert market_tools == server_tools
    assert len(market_tools) == 37
