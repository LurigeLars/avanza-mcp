"""Print deterministic MCP catalog and structured-output overhead measurements."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from fastmcp import Client, FastMCP
from pydantic import BaseModel

from avanza_mcp import mcp as avanza_mcp

ROOT = Path(__file__).resolve().parents[1]


class ProbeResult(BaseModel):
    symbol: str
    values: list[int]


probe = FastMCP("serialization-probe")


@probe.tool
def typed_probe() -> ProbeResult:
    return ProbeResult(symbol="TEST", values=list(range(20)))


@probe.tool(structured_output=False)
def unstructured_probe() -> str:
    return json.dumps(
        {"symbol": "TEST", "values": list(range(20))},
        separators=(",", ":"),
    )


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


def _bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


async def main() -> None:
    async with Client(avanza_mcp) as client:
        listed = await client.list_tools()
    tools = [_jsonable(item) for item in listed]

    compact = subprocess.run(
        ["node", str(ROOT / "scripts" / "measure_gateway_catalog.mjs")],
        input=json.dumps(tools, ensure_ascii=False, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=True,
    )
    catalog = json.loads(compact.stdout)

    async with Client(probe) as client:
        typed = _jsonable(await client.call_tool("typed_probe"))
        unstructured = _jsonable(await client.call_tool("unstructured_probe"))

    def result_shape(payload):
        structured = payload.get("structuredContent", payload.get("structured_content"))
        content = payload.get("content")
        return {
            "wire_bytes": _bytes(payload),
            "content_bytes": _bytes(content) if content is not None else 0,
            "structured_content_bytes": _bytes(structured) if structured is not None else 0,
            "has_content": content is not None,
            "has_structured_content": structured is not None,
        }

    screen = next(tool for tool in tools if tool["name"] == "screen_leveraged_instruments")
    report = {
        "catalog": {
            **catalog,
            "approx_raw_tokens_at_4_chars": round(catalog["raw_catalog_bytes"] / 4),
            "approx_compacted_tokens_at_4_chars": round(catalog["compacted_catalog_bytes"] / 4),
            "screen_has_output_schema_before_gateway": bool(screen.get("outputSchema")),
        },
        "fastmcp_serialization_probe": {
            "typed": result_shape(typed),
            "unstructured": result_shape(unstructured),
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
