"""Print deterministic MCP catalog and structured-output overhead measurements."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from fastmcp import Client, FastMCP
from pydantic import BaseModel

from avanza_mcp import mcp as avanza_mcp
from avanza_mcp.auth.browser import BrowserAuth
from avanza_mcp.auth.server import create_auth_server

ROOT = Path(__file__).resolve().parents[1]


class ProbeResult(BaseModel):
    symbol: str
    values: list[int]


probe = FastMCP("serialization-probe")


@probe.tool
def typed_probe() -> ProbeResult:
    return ProbeResult(symbol="TEST", values=list(range(20)))


@probe.tool
def unstructured_probe():
    return json.dumps(
        {"symbol": "TEST", "values": list(range(20))},
        separators=(",", ":"),
    )


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _compact_catalog(tools: list[dict]) -> dict:
    compact = subprocess.run(
        ["node", str(ROOT / "scripts" / "measure_gateway_catalog.mjs")],
        input=json.dumps(tools, ensure_ascii=False, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=True,
    )
    catalog = json.loads(compact.stdout)
    return {
        **catalog,
        "approx_raw_tokens_at_4_chars": round(catalog["raw_catalog_bytes"] / 4),
        "approx_compacted_tokens_at_4_chars": round(
            catalog["compacted_catalog_bytes"] / 4
        ),
    }


async def _list_tools(server) -> list[dict]:
    async with Client(server) as client:
        listed = await client.list_tools()
    return [_jsonable(item) for item in listed]


async def main() -> None:
    public_tools = await _list_tools(avanza_mcp)
    authenticated_tools = await _list_tools(
        create_auth_server(BrowserAuth(store=None))
    )

    public_catalog = _compact_catalog(public_tools)
    authenticated_catalog = _compact_catalog(authenticated_tools)

    async with Client(probe) as client:
        typed = _jsonable(await client.call_tool("typed_probe"))
        unstructured = _jsonable(await client.call_tool("unstructured_probe"))

    def result_shape(payload):
        content = getattr(payload, "content", None)
        structured = getattr(payload, "structured_content", None)
        normalized = {
            "content": _jsonable(content),
            "structuredContent": _jsonable(structured),
        }
        return {
            "wire_shape_bytes": _bytes(normalized),
            "content_bytes": _bytes(normalized["content"]) if content is not None else 0,
            "structured_content_bytes": (
                _bytes(normalized["structuredContent"])
                if structured is not None
                else 0
            ),
            "has_content": content is not None,
            "has_structured_content": structured is not None,
        }

    screen = next(
        tool for tool in public_tools if tool["name"] == "screen_leveraged_instruments"
    )
    report = {
        "catalog": {
            **public_catalog,
            "screen_has_output_schema_before_gateway": bool(screen.get("outputSchema")),
        },
        "authenticated_catalog": authenticated_catalog,
        "fastmcp_serialization_probe": {
            "typed": result_shape(typed),
            "unstructured": result_shape(unstructured),
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
