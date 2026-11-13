"""Bounded read-only timing probe. Prints no broker credentials/account data."""
import argparse
import asyncio
import json
import statistics
import time

from avanza_mcp.auth.broker import AuthProcessBroker
from avanza_mcp.capacity_bridge import CapacityReader, ID, MAX_IDS
from avanza_mcp.client.base import AvanzaClient


async def run(ids, authenticated):
    broker = AuthProcessBroker(mode="persistent") if authenticated else None
    auth_responses = 0
    async def delegate(method, path, kwargs):
        nonlocal auth_responses
        result = await broker.market_request(method, path, kwargs)
        if result is not None:
            auth_responses += 1
        return result
    try:
        if broker:
            await broker.warm_market_worker()
        async with AvanzaClient(authenticated_request_delegate=delegate if broker else None) as client:
            reader = CapacityReader(client)
            timings, statuses, ages = [], [], []
            for i in range(6):
                if i:
                    reader.cache.clear()
                start = time.perf_counter()
                result = await reader.batch(ids)
                timings.append(round((time.perf_counter() - start) * 1000, 2))
                statuses.append([q["status"] for q in result["items"].values()])
                ages.append([result["server_time"] - q["source_time"] for q in result["items"].values() if "source_time" in q])
            start = time.perf_counter()
            await reader.batch(ids)
            cache_ms = (time.perf_counter() - start) * 1000
            print(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "instruments": len(ids), "authenticated_requested": authenticated,
                              "authenticated_responses": auth_responses,
                              "cold_ms": timings[0], "warm_ms": timings[1:],
                              "warm_p50_ms": statistics.median(timings[1:]),
                              "warm_max_ms": max(timings[1:]), "cache_ms": round(cache_ms, 3),
                              "statuses": statuses, "source_ages_ms": ages}))
    finally:
        if broker:
            await broker.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="+")
    parser.add_argument("--authenticated", action="store_true", help="Use existing native-store session in isolated worker")
    args = parser.parse_args()
    if (not 1 <= len(args.ids) <= MAX_IDS or len(set(args.ids)) != len(args.ids)
            or any(not ID.fullmatch(i) for i in args.ids)):
        parser.error("Supply one to five unique numeric order-book IDs")
    asyncio.run(run(args.ids, args.authenticated))
