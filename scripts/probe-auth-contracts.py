"""Bounded local smoke probe for authenticated Avanza read contracts.

Requires the local MCP server to already have a connected Avanza session.
It never starts authentication and never prints account payloads.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from fastmcp import Client


_CHECKS: tuple[tuple[str, dict[str, object]], ...] = (
    ("get_accounts", {}),
    ("get_holdings", {}),
    ("get_transactions", {"limit": 1}),
    ("get_watchlists", {}),
    ("get_portfolio_snapshot", {"limit": 1}),
    ("get_active_orders", {"limit": 1}),
    ("get_deals", {"limit": 1}),
    ("get_stop_loss_orders", {"limit": 1}),
)


async def _run(url: str) -> int:
    async with Client(url) as client:
        status = await client.call_tool("get_auth_status", {})
        payload = status.structured_content or {}
        if payload.get("state") != "connected":
            print("SKIP: Avanza session is not connected.", file=sys.stderr)
            return 2

        failures: list[str] = []
        for name, arguments in _CHECKS:
            try:
                result = await client.call_tool(name, arguments)
                if result.structured_content is None:
                    raise RuntimeError("missing structured_content")
            except Exception as exc:
                failures.append(name)
                print(f"FAIL {name}: {type(exc).__name__}", file=sys.stderr)
            else:
                print(f"PASS {name}")

        if failures:
            print(
                "Authenticated contract probe failed: " + ", ".join(failures),
                file=sys.stderr,
            )
            return 1

    print(f"PASS authenticated contract probe ({len(_CHECKS)} reads)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8767/mcp",
        help="Local authenticated-capable Avanza MCP URL.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.url))


if __name__ == "__main__":
    raise SystemExit(main())
