"""Real worker-batch shape, bounded latency and per-instrument failure isolation."""
import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from avanza_mcp.auth import worker
from avanza_mcp.auth.broker import AuthProcessBroker
from avanza_mcp.capacity_bridge import CapacityReader
from avanza_mcp.client.bankid import SessionMaterial
from avanza_mcp.client.base import AvanzaClient
from avanza_mcp.client.exceptions import AvanzaAuthError, AvanzaRateLimitError


async def test_reader_uses_one_batch_and_keeps_partial_results():
    now = int(time.time() * 1000)
    depth = {"receivedTime": now, "marketMakerLevelInAsk": 1, "levels": [
        {"sellSide": {"price": 1, "volume": 999999}},
        {"sellSide": {"price": 10.5, "volume": 100}}]}
    client = AsyncMock()
    async def metadata(path):
        return {"orderbookId": path.rsplit("/", 1)[1], "listing": {"currency": "SEK"}}
    client.get_public.side_effect = metadata
    batch = AsyncMock(return_value=[depth, None, depth])
    reader = CapacityReader(client, batch)
    result = (await reader.batch(["1", "2", "3"]))["items"]
    batch.assert_awaited_once_with(["1", "2", "3"])
    client.get.assert_not_awaited()
    assert result["1"]["amount"] == "1050.0"
    assert result["2"]["status"] == "DEPTH_UNAVAILABLE"
    assert result["3"]["status"] == "OK"
    await reader.aclose()


async def test_depth_batch_client_preserves_quote_allowlist_and_order():
    delegate = AsyncMock(return_value=[{"levels": []}, None])
    async with AvanzaClient(authenticated_market_data_batch_delegate=delegate) as client:
        assert await client.get_authenticated_order_depth_batch(["1", "2"]) == [{"levels": []}, None]
        delegate.assert_awaited_once_with([
            "/_api/market-guide/stock/1/orderdepth", "/_api/market-guide/stock/2/orderdepth"])
        with pytest.raises(AvanzaAuthError):
            await client.get_authenticated_market_data_batch(["/_api/market-guide/stock/1/orderdepth"])
        with pytest.raises(ValueError):
            await client.get_authenticated_order_depth_batch(["1/../../../accounts"])
        with pytest.raises(ValueError):
            await client.get_authenticated_order_depth_batch(["1"] * 6)


async def test_broker_accepts_only_bounded_homogeneous_depth_paths(monkeypatch):
    broker = AuthProcessBroker(mode="persistent")
    command = AsyncMock(return_value={"ok": True, "result": [{"levels": []}, None]})
    monkeypatch.setattr(broker, "_persistent_market_command", command)
    try:
        paths = [f"/_api/market-guide/stock/{i}/orderdepth" for i in (1, 2)]
        assert await broker.market_data_batch(paths) == [{"levels": []}, None]
        command.assert_awaited_once_with({"action": "market_batch", "paths": paths})
        command.reset_mock()
        assert await broker.market_data_batch(paths * 3) is None
        assert await broker.market_data_batch(paths + ["/_api/trading-critical/rest/marketdata/3"]) is None
        assert await broker.market_data_batch(["/_api/trading/accounts"]) is None
        command.assert_not_awaited()
    finally:
        await broker.aclose()


async def test_worker_projects_depth_only_with_per_item_deadline():
    client = AsyncMock()
    client.request_authenticated_batch.return_value = [httpx.Response(200, json={
        "receivedTime": 123, "marketMakerLevelInAsk": 0,
        "levels": [{"sellSide": {"price": 2, "volume": 100, "accountId": "never-export"}}],
        "accountId": "never-export"}), None]
    auth = worker._RequestAuth(SessionMaterial((), "test-token"))
    paths = [f"/_api/market-guide/stock/{i}/orderdepth" for i in (1, 2)]
    result = await worker._market_batch_operation(auth, {"paths": paths}, client)
    assert result["ok"] is True
    assert result["result"][1] is None
    assert "never-export" not in str(result)
    assert result["result"][0]["levels"][0]["sellSide"] == {"price": 2, "volume": 100}
    client.request_authenticated_batch.assert_awaited_once_with(
        paths, max_concurrency=worker._MARKET_BATCH_CONCURRENCY, item_timeout=1.5)


async def test_slow_http_item_does_not_block_other_depths_or_cancel_session():
    closed = asyncio.Event()
    async def handler(request):
        if request.url.path.endswith("/slow"):
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, json={"levels": []})
    session = SessionMaterial((), "test-token")
    async with AvanzaClient(session_provider=lambda: session, min_request_interval=0, request_jitter=0) as client:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://www.avanza.se") as transport:
            client._authenticated_client = transport
            client._authenticated_session = session
            results = await client.request_authenticated_batch(["/slow", "/fast"], item_timeout=0.02)
            assert results[0] is None and results[1].status_code == 200
            assert closed.is_set()
            assert client._authenticated_session is session


async def test_depth_rate_limit_respects_retry_after_without_more_worker_calls(monkeypatch):
    broker = AuthProcessBroker(mode="persistent")
    command = AsyncMock(return_value={"ok": False, "code": "read_rate_limited", "retry_after": 120})
    monkeypatch.setattr(broker, "_persistent_market_command", command)
    try:
        for _ in range(2):
            with pytest.raises(AvanzaRateLimitError) as error:
                await broker.market_data_batch(["/_api/market-guide/stock/1/orderdepth"])
            assert error.value.retry_after >= 119
        command.assert_awaited_once()
    finally:
        await broker.aclose()


async def test_worker_carries_only_retry_after_from_rate_limit(monkeypatch):
    async with AvanzaClient() as client:
        monkeypatch.setattr(client, "request_authenticated_batch", AsyncMock(return_value=[
            httpx.Response(429, headers={"Retry-After": "120"}, json={"message": "limited"})]))
        auth = worker._RequestAuth(SessionMaterial((), "test-token"))
        result = await worker._market_batch_operation(auth, {"paths": ["/_api/market-guide/stock/1/orderdepth"]}, client)
        assert result == {"ok": False, "code": "read_rate_limited", "retry_after": 120}
