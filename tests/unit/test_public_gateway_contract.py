"""Keep the public gateway allowlist aligned with the MCP server contract."""

import re
from pathlib import Path

from fastmcp import Client

from avanza_mcp import mcp


async def test_public_gateway_allowlist_matches_current_read_only_tool_surface():
    policy = Path("public/gateway/policy.mjs").read_text(encoding="utf-8")
    match = re.search(
        r"export const DEFAULT_ALLOWED_TOOLS = \[(.*?)\]\.join\(','\);",
        policy,
        flags=re.DOTALL,
    )
    assert match, "DEFAULT_ALLOWED_TOOLS block not found"
    public_tools = set(re.findall(r"'([a-z][a-z0-9_]*)'", match.group(1)))

    async with Client(mcp) as client:
        server_tools = {tool.name for tool in await client.list_tools()}

    assert public_tools == server_tools
    assert len(public_tools) == 36
