"""Optional loopback-only certificate depth display; never an order API."""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import socket
import time

import keyring
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from .client.base import AvanzaClient
from .client.endpoints import PublicEndpoint
from .client.exceptions import AvanzaRateLimitError, AvanzaTimeoutError

PORT = 18762
MAX_AGE_MS = 5000
MAX_IDS = 5
ID = re.compile(r"[0-9]{1,12}\Z")
ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")
SERVICE = "avanza-mcp-capacity-bridge"


def capacity(depth: dict, currency: str | None, now_ms: int) -> dict:
    """Use only the indexed MM ask level, never best ask or summed levels."""
    def unavailable(status):
        result = {"status": status, "amount": None}
        if type(depth.get("receivedTime")) is int:
            result["source_time"] = depth["receivedTime"]
        return result

    if currency != "SEK":
        return unavailable("CURRENCY_UNKNOWN")
    stamp = depth.get("receivedTime")
    if type(stamp) is not int or not 0 <= now_ms - stamp <= MAX_AGE_MS:
        return unavailable("STALE")
    index, levels = depth.get("marketMakerLevelInAsk"), depth.get("levels")
    if (type(index) is not int or not isinstance(levels, list)
            or not 0 <= index < len(levels)):
        return unavailable("MM_MISSING")
    try:
        side = levels[index]["sellSide"]
        price, volume = side["price"], side["volume"]
        if (type(price) not in (int, float) or type(volume) not in (int, float)
                or not math.isfinite(price) or not math.isfinite(volume)
                or price <= 0 or volume <= 0 or int(volume) != volume):
            return unavailable("DEPTH_UNKNOWN")
        amount = Decimal(str(price)) * Decimal(str(volume))
        if not amount.is_finite() or amount > Decimal("90071992547409"):
            return unavailable("DEPTH_UNKNOWN")
    except (KeyError, TypeError, InvalidOperation):
        return unavailable("DEPTH_UNKNOWN")
    return {"status": "OK", "amount": str(amount), "price": price,
            "volume": volume, "source_time": stamp, "mm_level": index,
            "currency": "SEK"}


class CapacityReader:
    def __init__(self, client: AvanzaClient):
        self.client = client
        self.cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.metadata: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        self.pending: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def _read(self, book_id: str) -> dict:
        try:
            meta = self.metadata.get(book_id)
            if meta is None or time.monotonic() - meta[0] > 300:
                raw = await self.client.get_public(
                    PublicEndpoint.CERTIFICATE_INFO.format(id=book_id))
                currency = (raw.get("listing") or {}).get("currency")
                if str(raw.get("orderbookId")) != book_id:
                    currency = None
                self.metadata[book_id] = (time.monotonic(), currency)
                while len(self.metadata) > 200:
                    self.metadata.popitem(last=False)
            else:
                currency = meta[1]
            depth = await self.client.get(
                PublicEndpoint.STOCK_ORDERDEPTH.format(id=book_id))
            result = capacity(depth, currency, int(time.time() * 1000))
        except AvanzaRateLimitError:
            result = {"status": "RATE_LIMITED", "amount": None}
        except (TimeoutError, AvanzaTimeoutError):
            result = {"status": "TIMEOUT", "amount": None}
        except Exception:
            # Never return upstream exception text (may contain session material).
            result = {"status": "OFFLINE", "amount": None}
        self.cache[book_id] = (time.monotonic(), result)
        self.cache.move_to_end(book_id)
        while len(self.cache) > 200:
            self.cache.popitem(last=False)
        return result

    async def _bounded_read(self, book_id):
        task = self.pending.get(book_id)
        if task is None:
            if len(self.pending) >= MAX_IDS:
                return {"status": "BUSY", "amount": None}
            task = asyncio.create_task(self._read(book_id))
            self.pending[book_id] = task
            task.add_done_callback(lambda _: self.pending.pop(book_id, None))
        try:
            async with asyncio.timeout(2):
                # Cancelling shared broker IPC retires its memory-only session.
                return await asyncio.shield(task)
        except TimeoutError:
            return {"status": "TIMEOUT", "amount": None}

    async def aclose(self):
        # Only the owning MCP lifespan's shutdown may cancel these reads.
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def batch(self, ids: list[str]) -> dict:
        # One bounded upstream batch at a time also coalesces concurrent tabs.
        async with self.lock:
            values = {}
            missing = []
            for book_id in ids:
                cached = self.cache.get(book_id)
                if cached and time.monotonic() - cached[0] < 0.75:
                    values[book_id] = cached[1]
                else:
                    missing.append(book_id)
            results = await asyncio.gather(*(self._bounded_read(i) for i in missing))
            values.update(zip(missing, results))
            now = int(time.time() * 1000)
            for book_id, value in list(values.items()):
                if value["status"] == "OK" and not 0 <= now - value["source_time"] <= MAX_AGE_MS:
                    values[book_id] = {"status": "STALE", "amount": None}
            return {"items": values, "server_time": now, "max_age_ms": MAX_AGE_MS}


class Pairing:
    """Native credential store; only hashes persist, code is single-use."""
    def __init__(self, backend=keyring):
        self.backend = backend

    def load(self):
        value = self.backend.get_password(SERVICE, "pairing")
        return json.loads(value) if value else {}

    def save(self, value):
        self.backend.set_password(SERVICE, "pairing", json.dumps(value))

    def issue(self):
        code = secrets.token_hex(10).upper()
        value = self.load()
        value.update(code_hash=self.digest(code), expires=time.time() + 600)
        self.save(value)
        return code

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    def pair(self, origin, code):
        value = self.load()
        if (not isinstance(code, str) or not re.fullmatch(r"[A-F0-9]{20}", code)
                or time.time() >= value.get("expires", 0)
                or not hmac.compare_digest(self.digest(code), value.get("code_hash", ""))):
            return None
        token = secrets.token_hex(32)
        self.save({"origin": origin, "token_hash": self.digest(token)})
        return token

    def authorized(self, origin, token):
        value = self.load()
        return (isinstance(token, str) and re.fullmatch(r"[a-f0-9]{64}", token)
                and origin == value.get("origin")
                and hmac.compare_digest(self.digest(token), value.get("token_hash", "")))


def create_app(reader: CapacityReader, pairing: Pairing, port=PORT):
    hits = deque()
    pair_hits = deque()
    slots = asyncio.Semaphore(4)

    async def endpoint(request: Request):
        origin = request.headers.get("origin", "")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

        def reply(status, body):
            return JSONResponse(body, status_code=status, headers=headers)

        if (request.headers.get("host") != f"127.0.0.1:{port}"
                or not ORIGIN.fullmatch(origin)):
            return reply(403, {"status": "DENIED"})
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
        now = time.monotonic()
        while hits and hits[0] < now - 60:
            hits.popleft()
        if len(hits) >= 120:
            return reply(429, {"status": "RATE_LIMITED"})
        hits.append(now)
        if request.method == "OPTIONS":
            headers.update({"Access-Control-Allow-Methods": "POST, OPTIONS",
                            "Access-Control-Allow-Headers": "Content-Type, X-Avanza-Capacity, X-Avanza-Token",
                            "Access-Control-Max-Age": "600"})
            return reply(200, {})
        if (request.headers.get("x-avanza-capacity") != "1"
                or request.headers.get("content-type", "").split(";")[0] != "application/json"
                or request.headers.get("transfer-encoding")):
            return reply(403, {"status": "DENIED"})
        try:
            size = int(request.headers.get("content-length", "0"))
            if not 0 < size <= 1024:
                return reply(413, {"status": "INVALID_SIZE"})
            if request.url.path == "/capacity" and not pairing.authorized(
                    origin, request.headers.get("x-avanza-token")):
                return reply(401, {"status": "PAIR_REQUIRED"})
            async with asyncio.timeout(1):
                body = bytearray()
                async for part in request.stream():
                    body.extend(part)
                    if len(body) > size:
                        return reply(413, {"status": "INVALID_SIZE"})
                if len(body) != size:
                    return reply(400, {"status": "INVALID"})
                data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            if request.url.path == "/pair":
                while pair_hits and pair_hits[0] < now - 600:
                    pair_hits.popleft()
                if len(pair_hits) >= 5:
                    return reply(429, {"status": "PAIR_LIMITED"})
                pair_hits.append(now)
                token = pairing.pair(origin, data.get("code"))
                return reply(200, {"status": "PAIRED", "token": token}) if token else reply(403, {"status": "PAIR_REQUIRED"})
            ids = data.get("ids")
            if (set(data) != {"ids"} or not isinstance(ids, list)
                    or not 1 <= len(ids) <= MAX_IDS
                    or any(not isinstance(i, str) or not ID.fullmatch(i) for i in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError()
            if slots.locked():
                return reply(429, {"status": "BUSY"})
            async with slots, asyncio.timeout(3):
                return reply(200, await reader.batch(ids))
        except (ValueError, UnicodeError):
            return reply(400, {"status": "INVALID"})
        except TimeoutError:
            return reply(504, {"status": "TIMEOUT"})
        except Exception:
            return reply(503, {"status": "OFFLINE"})

    return Starlette(routes=[Route(p, endpoint, methods=["POST", "OPTIONS"])
                             for p in ("/pair", "/capacity")])


class _EmbeddedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The owning MCP server controls process shutdown.
        yield


@asynccontextmanager
async def serve_bridge(client, *, port=PORT):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(8)
    except OSError:
        listener.close()
        logging.getLogger(__name__).warning("Capacity bridge OFFLINE: loopback port unavailable")
        yield
        return
    reader = CapacityReader(client)
    config = uvicorn.Config(create_app(reader, Pairing(), port=port),
                            host="127.0.0.1", port=port, access_log=False,
                            log_config=None, log_level="warning", limit_concurrency=8,
                            timeout_keep_alive=2, timeout_graceful_shutdown=3, ws="none")
    server = _EmbeddedServer(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Capacity bridge failed to start")
            await asyncio.sleep(0.01)
        yield
    finally:
        server.should_exit = True
        try:
            await task
        finally:
            try:
                await reader.aclose()
            finally:
                listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pair", "serve-public"])
    args = parser.parse_args()
    if args.command == "pair":
        print("One-time capacity bridge code (10 minutes; keep private):")
        print(Pairing().issue())
    else:
        async def run():
            async with AvanzaClient() as client, serve_bridge(client):
                await asyncio.Event().wait()
        asyncio.run(run())


if __name__ == "__main__":
    main()
