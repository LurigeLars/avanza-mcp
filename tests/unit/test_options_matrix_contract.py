import pytest
from fastmcp import Client

from avanza_mcp import mcp


@pytest.mark.asyncio
async def test_futures_forwards_matrix_exposes_call_put_filter():
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    props = tools["list_futures_forwards"].input_schema["properties"]
    assert "call_indicators" in props
    values = props["call_indicators"]["anyOf"][0]["items"]["enum"]
    assert values == ["CALL", "PUT"]
