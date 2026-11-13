"""Money, freshness, bounded reads and the loopback trust boundary."""
import asyncio
import copy
import json
import signal
import socket
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from avanza_mcp.capacity_bridge import CapacityReader, Pairing, capacity, create_app, serve_bridge
from avanza_mcp.client.exceptions import AvanzaRateLimitError

NOW = 1800000000000
ORIGIN = "chrome-extension://" + "a" * 32


def depth():
    return {"receivedTime": NOW, "marketMakerLevelInAsk": 1, "levels": [
        {"sellSide": {"price": 1, "volume": 9000000}},
        {"sellSide": {"price": 263.36, "volume": 1000}}]}


def test_uses_mm_index_not_best_ask_or_sum():
    result = capacity(depth(), "SEK", NOW)
    assert result["amount"] == "263360.00"
    assert result["mm_level"] == 1


@pytest.mark.parametrize("field,value", [
    ("marketMakerLevelInAsk", None), ("marketMakerLevelInAsk", -1),
    ("marketMakerLevelInAsk", True), ("marketMakerLevelInAsk", 2),
    ("marketMakerLevelInAsk", 0.0), ("receivedTime", None),
    ("receivedTime", NOW - 8001), ("receivedTime", NOW + 1),
    ("receivedTime", True), ("levels", []), ("levels", None)])
def test_uncertain_depth_fails_closed(field, value):
    raw = depth(); raw[field] = value
    assert capacity(raw, "SEK", NOW)["amount"] is None


@pytest.mark.parametrize("field,value", [
    ("price", 0), ("price", -1), ("price", float("nan")),
    ("price", float("inf")), ("price", "1,2"), ("price", True),
    ("volume", 0), ("volume", None), ("volume", 1.5), ("volume", True)])
def test_bad_side_fails_closed(field, value):
    raw = depth(); raw["levels"][1]["sellSide"][field] = value
    assert capacity(raw, "SEK", NOW)["amount"] is None


def test_currency_never_assumed_and_input_not_mutated():
    raw = depth(); original = copy.deepcopy(raw)
    assert capacity(raw, None, NOW)["status"] == "CURRENCY_UNKNOWN"
    assert capacity(raw, "EUR", NOW)["amount"] is None
    assert raw == original


class Backend:
    value = None

    def get_password(self, *_): return self.value
    def set_password(self, *args): self.value = args[-1]


def paired():
    store = Pairing(Backend())
    code = store.issue()
    token = store.pair(ORIGIN, code)
    return store, code, token


def test_pairing_single_use_pinned_origin_rotation_and_hash_only():
    store, code, token = paired()
    assert store.authorized(ORIGIN, token)
    assert not store.authorized("chrome-extension://" + "b" * 32, token)
    assert store.pair(ORIGIN, code) is None
    assert token not in store.backend.value
    code = store.issue()
    assert code not in store.backend.value
    new = store.pair(ORIGIN, code)
    assert store.authorized(ORIGIN, new)
    assert not store.authorized(ORIGIN, token)
    store.issue()
    value = store.load(); value["expires"] = 0; store.save(value)
    assert store.pair(ORIGIN, code) is None


async def test_cache_coalesces_concurrent_tabs_and_rechecks_source_age(monkeypatch):
    clock = [NOW / 1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    client = AsyncMock()
    client.get_public.return_value = {"orderbookId": "123", "listing": {"currency": "SEK"}}
    client.get.return_value = depth()
    reader = CapacityReader(client)
    a, b = await asyncio.gather(reader.batch(["123"]), reader.batch(["123"]))
    assert a["items"] == b["items"]
    assert client.get.await_count == 1
    assert client.get_public.await_count == 1
    clock[0] += 6
    assert (await reader.batch(["123"]))["items"]["123"]["status"] == "OK"
    clock[0] += 3
    result = (await reader.batch(["123"]))["items"]["123"]
    assert result["status"] == "STALE" and result["amount"] is None


async def test_failures_do_not_reuse_success_or_leak_exception(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)
    client = AsyncMock()
    client.get_public.return_value = {"orderbookId": "123", "listing": {"currency": "SEK"}}
    client.get.return_value = depth()
    reader = CapacityReader(client)
    assert (await reader.batch(["123"]))["items"]["123"]["status"] == "OK"
    reader.cache.clear()
    client.get.side_effect = RuntimeError("sensitive-upstream-text")
    result = await reader.batch(["123"])
    assert result["items"]["123"] == {"status": "OFFLINE", "amount": None}
    assert "sensitive" not in json.dumps(result)


async def test_timeout_preserves_shared_session_and_pending_reads_are_bounded(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)
    client = AsyncMock()
    async def metadata(path):
        return {"orderbookId": path.rsplit("/", 1)[1], "listing": {"currency": "SEK"}}
    client.get_public.side_effect = metadata
    cancelled = asyncio.Event()
    release = asyncio.Event()
    async def blocked(*_):
        try:
            await release.wait()
            return depth()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    client.get.side_effect = blocked
    reader = CapacityReader(client)
    ids = [str(i) for i in range(5)]
    result = await reader.batch(ids)
    assert all(q["status"] == "TIMEOUT" for q in result["items"].values())
    assert not cancelled.is_set()
    assert len(reader.pending) == 5
    assert (await reader.batch(["999"]))["items"]["999"]["status"] == "BUSY"
    assert client.get.await_count == 5
    # A cancelled HTTP request must not retire the shared auth worker either.
    waiting = asyncio.create_task(reader.batch(ids))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not cancelled.is_set()
    release.set()
    await asyncio.gather(*reader.pending.values())
    assert not cancelled.is_set()
    assert (await reader.batch(ids))["items"]["0"]["status"] == "OK"
    reader.cache.clear()
    client.get.side_effect = AvanzaRateLimitError(30)
    assert (await reader.batch(["0"]))["items"]["0"]["status"] == "RATE_LIMITED"
    await reader.aclose()


async def test_partial_batch_preserves_healthy_instrument(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)
    client = AsyncMock()
    async def metadata(path):
        return {"orderbookId": path.rsplit("/", 1)[1], "listing": {"currency": "SEK"}}
    async def read(path):
        if "/456/" in path:
            raise OSError()
        return depth()
    client.get_public.side_effect = metadata
    client.get.side_effect = read
    result = (await CapacityReader(client).batch(["123", "456"]))["items"]
    assert result["123"]["status"] == "OK"
    assert result["456"]["status"] == "OFFLINE"


async def test_metadata_identity_mismatch_and_cache_bound(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)
    client = AsyncMock()
    client.get_public.return_value = {"orderbookId": "999", "listing": {"currency": "SEK"}}
    client.get.return_value = depth()
    reader = CapacityReader(client)
    for i in range(205):
        await reader.batch([str(i)])
    assert len(reader.cache) == len(reader.metadata) == 200
    assert reader.cache["204"][1]["amount"] is None


@pytest.fixture
async def api():
    store, _, token = paired()
    reader = AsyncMock()
    reader.batch.return_value = {"items": {"123": {"status": "MM_MISSING", "amount": None}}}
    app = create_app(reader, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:18762") as client:
        yield client, reader, store, token


def headers(token):
    return {"Origin": ORIGIN, "X-Avanza-Capacity": "1", "X-Avanza-Token": token}


async def test_valid_only_fixed_read_route_and_cors(api):
    client, reader, _, token = api
    result = await client.post("/capacity", json={"ids": ["123"]}, headers=headers(token))
    assert result.status_code == 200
    assert result.headers["access-control-allow-origin"] == ORIGIN
    assert result.headers["cache-control"] == "no-store"
    reader.batch.assert_awaited_once_with(["123"])
    assert (await client.get("/capacity", headers=headers(token))).status_code == 405
    assert (await client.post("/orders", json={}, headers=headers(token))).status_code == 404


@pytest.mark.parametrize("change", [
    {"Origin": "https://www.avanza.se"}, {"Host": "evil.example"},
    {"Origin": "null"}, {"X-Avanza-Token": "0" * 64},
    {"Origin": "chrome-extension://" + "b" * 32},
    {"X-Avanza-Capacity": "0"}, {"Content-Type": "text/plain"},
    {"Transfer-Encoding": "chunked"}])
async def test_trust_boundary_rejects_before_upstream(api, change):
    client, reader, _, token = api
    result = await client.post("/capacity", json={"ids": ["123"]}, headers=headers(token) | change)
    assert result.status_code in (401, 403)
    reader.batch.assert_not_awaited()


@pytest.mark.parametrize("body", [
    {"ids": []}, {"ids": ["1"] * 2}, {"ids": [str(i) for i in range(6)]},
    {"ids": ["../orders"]}, {"ids": ["１２３"]}, {"ids": [123]},
    {"ids": ["123"], "path": "/accounts"}, []])
async def test_bounded_numeric_unique_ids(api, body):
    client, reader, _, token = api
    assert (await client.post("/capacity", json=body, headers=headers(token))).status_code == 400
    reader.batch.assert_not_awaited()


async def test_body_cap_pair_limit_and_no_token_in_read_response(api):
    client, reader, store, token = api
    result = await client.post("/capacity", content=b"x" * 1025,
                              headers=headers(token) | {"Content-Type": "application/json"})
    assert result.status_code == 413
    for _ in range(5):
        assert (await client.post("/pair", json={"code": "0" * 20}, headers=headers(token))).status_code == 403
    assert (await client.post("/pair", json={"code": store.issue()}, headers=headers(token))).status_code == 429
    reader.batch.assert_not_awaited()


async def test_pair_exchange_then_old_token_revoked(api):
    client, _, store, old = api
    result = await client.post("/pair", json={"code": store.issue()}, headers=headers(old))
    assert result.status_code == 200
    new = result.json()["token"]
    assert store.authorized(ORIGIN, new)
    assert not store.authorized(ORIGIN, old)


async def test_preflight_is_narrow(api):
    client, reader, _, _ = api
    result = await client.options("/capacity", headers={"Origin": ORIGIN})
    assert result.status_code == 200
    assert result.headers["access-control-allow-methods"] == "POST, OPTIONS"
    assert "*" not in result.headers["access-control-allow-origin"]
    reader.batch.assert_not_awaited()


async def test_optional_listener_keeps_owner_signal_handlers_and_closes():
    before = signal.getsignal(signal.SIGINT)
    async with serve_bridge(AsyncMock(), port=0):
        assert signal.getsignal(signal.SIGINT) is before
    assert signal.getsignal(signal.SIGINT) is before


async def test_port_conflict_keeps_mcp_running(caplog):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0)); listener.listen()
        async with serve_bridge(AsyncMock(), port=listener.getsockname()[1]):
            assert "Capacity bridge OFFLINE" in caplog.text
