"""Live benchmark for bounded leveraged-product aggregation.

Run during market hours when spread/bid/ask availability matters. This script is
read-only and calls only the same public filter endpoints as the MCP tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from avanza_mcp.client import AvanzaClient
from avanza_mcp.services.leveraged_screen_service import LeveragedScreenService


def _availability(products: list[dict]) -> dict[str, int]:
    keys = (
        "leverage",
        "upstream_spread",
        "spread_percent_from_discovery_prices",
        "discovery_bid",
        "discovery_ask",
        "total_value_traded",
        "stop_loss",
    )
    return {
        key: sum(product.get(key) is not None for product in products)
        for key in keys
    }


async def _run(
    underlying_order_book_id: str,
    direction: str,
    max_per_type: int,
    repeats: int,
) -> dict:
    sequential_ms: list[float] = []
    aggregate_ms: list[float] = []
    latest = None

    async with AvanzaClient() as client:
        service = LeveragedScreenService(client)

        # Warm one request path so DNS/TLS setup does not dominate one side.
        await service.screen(
            underlying_order_book_id,
            direction,
            ["certificate", "warrant"],
            min(max_per_type, 10),
        )

        for index in range(repeats):
            if index % 2 == 0:
                start = time.perf_counter()
                certificate = await service._collect_certificates(
                    underlying_order_book_id, direction, max_per_type
                )
                warrant = await service._collect_warrants(
                    underlying_order_book_id, direction, max_per_type
                )
                sequential_ms.append((time.perf_counter() - start) * 1000)

                start = time.perf_counter()
                latest = await service.screen(
                    underlying_order_book_id,
                    direction,
                    ["certificate", "warrant"],
                    max_per_type,
                )
                aggregate_ms.append((time.perf_counter() - start) * 1000)
            else:
                start = time.perf_counter()
                latest = await service.screen(
                    underlying_order_book_id,
                    direction,
                    ["certificate", "warrant"],
                    max_per_type,
                )
                aggregate_ms.append((time.perf_counter() - start) * 1000)

                start = time.perf_counter()
                certificate = await service._collect_certificates(
                    underlying_order_book_id, direction, max_per_type
                )
                warrant = await service._collect_warrants(
                    underlying_order_book_id, direction, max_per_type
                )
                sequential_ms.append((time.perf_counter() - start) * 1000)

    assert latest is not None

    def stats(values: list[float]) -> dict[str, float]:
        return {
            "min_ms": round(min(values), 1),
            "median_ms": round(statistics.median(values), 1),
            "mean_ms": round(statistics.mean(values), 1),
            "max_ms": round(max(values), 1),
        }

    return {
        "underlying_order_book_id": underlying_order_book_id,
        "direction": direction,
        "max_per_type": max_per_type,
        "repeats": repeats,
        "sequential_certificate_then_warrant": stats(sequential_ms),
        "aggregate_concurrent_families": stats(aggregate_ms),
        "latest_family_counts": latest["families"],
        "latest_field_availability": _availability(latest["products"]),
        "latest_products_returned": latest["returned"],
        "note": (
            "Availability is observational. Closed or illiquid markets may legitimately "
            "return missing/stale discovery bid, ask or spread fields."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying-order-book-id", default="4478")
    parser.add_argument("--direction", choices=("long", "short"), default="long")
    parser.add_argument("--max-per-type", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                _run(
                    args.underlying_order_book_id,
                    args.direction,
                    args.max_per_type,
                    args.repeats,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
