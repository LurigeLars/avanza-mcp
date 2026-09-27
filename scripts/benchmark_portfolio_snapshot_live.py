"""Benchmark one authenticated portfolio snapshot against equivalent separate MCP calls.

The script talks only to the loopback FastMCP endpoint, prints structural counts and
latency, and never prints account, holding, order, deal, or stop-loss payload values.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any

from fastmcp import Client

DEFAULT_URL = "http://127.0.0.1:8767/mcp"
SUSPICIOUS = (
    "cookie",
    "token",
    "security",
    "session",
    "credential",
    "bankid",
    "authorization",
    "password",
    "secret",
)

SEPARATE_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("get_accounts", {}),
    ("get_holdings", {}),
    ("get_active_orders", {"limit": 100}),
    ("get_deals", {"limit": 100}),
    ("get_stop_loss_orders", {"limit": 100}),
)


def _jsonable_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None)
    if isinstance(content, list) and len(content) == 1:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise RuntimeError("Unexpected MCP result shape")


def _collect_keys(value: Any, output: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            output.add(str(key))
            _collect_keys(item, output)
    elif isinstance(value, list):
        for item in value:
            _collect_keys(item, output)


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    keys: set[str] = set()
    _collect_keys(payload, keys)
    return {
        "counts": {
            key: len(value)
            for key, value in payload.items()
            if isinstance(value, list)
        },
        "suspicious_field_names": sorted(
            key for key in keys if any(term in key.lower() for term in SUSPICIOUS)
        ),
    }


async def _timed_call(
    client: Client,
    tool: str,
    arguments: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    started = time.perf_counter()
    result = await client.call_tool(tool, arguments)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return elapsed_ms, _jsonable_result(result)


async def _sample(url: str) -> dict[str, Any]:
    async with Client(url) as client:
        snapshot_ms, snapshot = await _timed_call(
            client, "get_portfolio_snapshot", {"limit": 100}
        )

        separate_started = time.perf_counter()
        separate: dict[str, dict[str, Any]] = {}
        separate_ms: dict[str, float] = {}
        for tool, arguments in SEPARATE_CALLS:
            elapsed, payload = await _timed_call(client, tool, arguments)
            separate_ms[tool] = round(elapsed, 1)
            separate[tool] = payload
        separate_total_ms = (time.perf_counter() - separate_started) * 1000

    combined_keys: set[str] = set()
    for payload in separate.values():
        _collect_keys(payload, combined_keys)

    snapshot_summary = _summary(snapshot)
    return {
        "snapshot_ms": round(snapshot_ms, 1),
        "separate_total_ms": round(separate_total_ms, 1),
        "separate_call_ms": separate_ms,
        "speedup_x": round(separate_total_ms / snapshot_ms, 2)
        if snapshot_ms > 0
        else None,
        "saved_ms": round(separate_total_ms - snapshot_ms, 1),
        "snapshot": snapshot_summary,
        "separate_suspicious_field_names": sorted(
            key
            for key in combined_keys
            if any(term in key.lower() for term in SUSPICIOUS)
        ),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--samples", type=int, default=1, choices=range(1, 6))
    args = parser.parse_args()

    samples = [await _sample(args.url) for _ in range(args.samples)]
    report: dict[str, Any] = {"samples": samples}

    if len(samples) > 1:
        snapshot_values = [item["snapshot_ms"] for item in samples]
        separate_values = [item["separate_total_ms"] for item in samples]
        report["median"] = {
            "snapshot_ms": round(statistics.median(snapshot_values), 1),
            "separate_total_ms": round(statistics.median(separate_values), 1),
            "speedup_x": round(
                statistics.median(separate_values)
                / statistics.median(snapshot_values),
                2,
            ),
        }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
