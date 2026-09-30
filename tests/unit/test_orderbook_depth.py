"""Unit tests for authenticated multi-level order-depth SSE snapshots."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from avanza_mcp.auth.broker import (
    AuthProcessBroker,
    AuthWorkerExpired,
    AuthWorkerRequired,
)
from avanza_mcp.auth.server import create_auth_server
from avanza_mcp.client.bankid import SessionMaterial
from avanza_mcp.client.base import (
    AvanzaClient,
    _project_rest_order_depth,
)
from avanza_mcp.client.exceptions import (
    AvanzaAuthError,
    AvanzaNetworkError,
    AvanzaTimeoutError,
)


ORDER_BOOK_ID = "741117"
STREAM_URL = (
    "https://www.avanza.se/_push/order-depth-web-push/" + ORDER_BOOK_ID
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        yield b"event: info\ndata: heartbeat\n\n"
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


def sse_response(*chunks: bytes, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "text/event-stream"},
        stream=ChunkStream(*chunks),
    )


async def read_depth(
    response: httpx.Response,
    *,
    max_levels: int = 10,
    timeout: float = 1.0,
    invalidated: AsyncMock | None = None,
):
    session = SessionMaterial((), "test-token")
    async with AvanzaClient(
        session_provider=lambda: session,
        session_invalidated=invalidated,
        min_request_interval=0,
        request_jitter=0,
    ) as client:
        return await client.get_authenticated_order_depth_snapshot(
            ORDER_BOOK_ID,
            max_levels=max_levels,
            timeout=timeout,
        )


@respx.mock
async def test_order_depth_ignores_info_then_returns_order_depth_snapshot():
    payload = {
        "orderbookId": ORDER_BOOK_ID,
        "receivedTime": 1790712000123,
        "levels": [
            {
                "buyPrice": 10.0,
                "buyVolume": 500,
                "sellPrice": 10.15,
                "sellVolume": 25000,
            }
        ],
        "marketMakerLevelInAsk": 0,
        "marketMakerLevelInBid": 1,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "text/event-stream"
        assert request.headers["aza-do-not-touch-session"] == "true"
        assert request.headers["Referer"].endswith("/" + ORDER_BOOK_ID)
        assert request.headers["Sec-Fetch-Dest"] == "empty"
        assert request.headers["Sec-Fetch-Mode"] == "cors"
        assert request.headers["Sec-Fetch-Site"] == "same-origin"
        assert request.headers["Sec-Ch-Ua-Mobile"] == "?0"
        assert request.headers["Sec-Ch-Ua-Platform"] == '"macOS"'
        assert "Mozilla/5.0" in request.headers["User-Agent"]
        assert request.headers["X-SecurityToken"] == "test-token"
        return sse_response(
            b"event: info\ndata: connected\nid: i-1\nretry: 1000\n\n",
            (
                "event: ORDER_DEPTH\ndata: "
                + json.dumps(payload)
                + "\nid: d-1\nretry: 1000\n\n"
            ).encode(),
        )

    respx.get(STREAM_URL).mock(side_effect=handler)
    result = await read_depth(httpx.Response(200))

    assert result == {
        "orderBookId": ORDER_BOOK_ID,
        "receivedTime": 1790712000123,
        "levels": [
            {
                "buyPrice": 10.0,
                "buyVolume": 500.0,
                "sellPrice": 10.15,
                "sellVolume": 25000.0,
            }
        ],
        "marketMakerLevelInAsk": 0,
        "marketMakerLevelInBid": 1,
    }


@respx.mock
async def test_order_depth_preserves_three_levels():
    levels = [
        {"buyPrice": 70.84, "buyVolume": 1200, "sellPrice": 70.86, "sellVolume": 800},
        {"buyPrice": 70.82, "buyVolume": 2500, "sellPrice": 70.88, "sellVolume": 1500},
        {"buyPrice": 70.80, "buyVolume": 400, "sellPrice": 70.90, "sellVolume": 700},
    ]
    payload = {
        "orderbookId": ORDER_BOOK_ID,
        "levels": levels,
        "marketMakerLevelInAsk": 0,
        "marketMakerLevelInBid": 1,
    }
    respx.get(STREAM_URL).mock(
        return_value=sse_response(
            ("event: ORDER_DEPTH\ndata: " + json.dumps(payload) + "\n\n").encode()
        )
    )

    result = await read_depth(httpx.Response(200))
    assert len(result["levels"]) == 3
    assert [level["buyPrice"] for level in result["levels"]] == [70.84, 70.82, 70.8]
    assert [level["sellVolume"] for level in result["levels"]] == [800.0, 1500.0, 700.0]


@respx.mock
async def test_order_depth_max_levels_only_slices_without_aggregation():
    levels = [
        {"buyPrice": 10 - i * 0.01, "buyVolume": 100 + i, "sellPrice": 10.1 + i * 0.01, "sellVolume": 200 + i}
        for i in range(4)
    ]
    payload = {"orderbookId": ORDER_BOOK_ID, "levels": levels}
    respx.get(STREAM_URL).mock(
        return_value=sse_response(
            ("event: ORDER_DEPTH\ndata: " + json.dumps(payload) + "\n\n").encode()
        )
    )

    result = await read_depth(httpx.Response(200), max_levels=2)
    assert result["levels"] == [
        {
            "buyPrice": 10.0,
            "buyVolume": 100.0,
            "sellPrice": 10.1,
            "sellVolume": 200.0,
        },
        {
            "buyPrice": 9.99,
            "buyVolume": 101.0,
            "sellPrice": 10.11,
            "sellVolume": 201.0,
        },
    ]


@respx.mock
async def test_order_depth_missing_side_is_null_not_zero_order():
    payload = {
        "orderbookId": ORDER_BOOK_ID,
        "levels": [
            {
                "buyPrice": 70.8,
                "buyVolume": 400,
                "sellPrice": 0,
                "sellVolume": 0,
            },
            {
                "buyPrice": None,
                "buyVolume": None,
                "sellPrice": 70.9,
                "sellVolume": 700,
            },
        ],
    }
    respx.get(STREAM_URL).mock(
        return_value=sse_response(
            ("event: ORDER_DEPTH\ndata: " + json.dumps(payload) + "\n\n").encode()
        )
    )

    result = await read_depth(httpx.Response(200))
    assert result["levels"][0]["sellPrice"] is None
    assert result["levels"][0]["sellVolume"] is None
    assert result["levels"][1]["buyPrice"] is None
    assert result["levels"][1]["buyVolume"] is None


@respx.mock
async def test_order_depth_timeout_closes_stream():
    stream = BlockingStream()
    respx.get(STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
        )
    )

    with pytest.raises(AvanzaTimeoutError):
        await read_depth(httpx.Response(200), timeout=0.05)
    assert stream.closed is True


@respx.mock
async def test_order_depth_malformed_json_fails_closed():
    respx.get(STREAM_URL).mock(
        return_value=sse_response(b"event: ORDER_DEPTH\ndata: {not-json}\n\n")
    )
    with pytest.raises(ValueError, match="Malformed ORDER_DEPTH JSON"):
        await read_depth(httpx.Response(200))


@respx.mock
async def test_order_depth_server_disconnect_before_snapshot():
    respx.get(STREAM_URL).mock(
        return_value=sse_response(b"event: info\ndata: heartbeat\n\n")
    )
    with pytest.raises(AvanzaNetworkError, match="closed"):
        await read_depth(httpx.Response(200))


@respx.mock
async def test_order_depth_cancellation_closes_stream_socket():
    stream = BlockingStream()
    respx.get(STREAM_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
        )
    )
    session = SessionMaterial((), "test-token")

    async with AvanzaClient(
        session_provider=lambda: session,
        min_request_interval=0,
        request_jitter=0,
    ) as client:
        task = asyncio.create_task(
            client.get_authenticated_order_depth_snapshot(
                ORDER_BOOK_ID, timeout=10
            )
        )
        await stream.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await task

    assert stream.closed is True


@respx.mock
async def test_order_depth_auth_missing_and_expired():
    async with AvanzaClient(
        session_provider=lambda: None,
        min_request_interval=0,
        request_jitter=0,
    ) as client:
        with pytest.raises(AvanzaAuthError, match="No authenticated"):
            await client.get_authenticated_order_depth_snapshot(ORDER_BOOK_ID)
    assert not respx.calls

    invalidated = AsyncMock()
    respx.get(STREAM_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(AvanzaAuthError, match="denied"):
        await read_depth(
            httpx.Response(401),
            invalidated=invalidated,
        )
    invalidated.assert_awaited_once()


@respx.mock
async def test_order_depth_projection_cannot_leak_auth_or_session_fields():
    payload = {
        "orderbookId": ORDER_BOOK_ID,
        "securityToken": "must-not-leak",
        "accountId": "must-not-leak",
        "levels": [
            {
                "buyPrice": 10,
                "buyVolume": 100,
                "sellPrice": 10.1,
                "sellVolume": 200,
                "cookies": ["must-not-leak"],
            }
        ],
        "authenticationSession": "must-not-leak",
    }
    respx.get(STREAM_URL).mock(
        return_value=sse_response(
            ("event: ORDER_DEPTH\ndata: " + json.dumps(payload) + "\n\n").encode()
        )
    )

    result = await read_depth(httpx.Response(200))
    serialized = json.dumps(result)
    assert "must-not-leak" not in serialized
    assert "securityToken" not in serialized
    assert "accountId" not in serialized
    assert "cookies" not in serialized


def test_reusable_rest_order_depth_projection_strips_sensitive_fields():
    projected = _project_rest_order_depth(
        {
            "receivedTime": 123,
            "marketMakerExpected": True,
            "levels": [
                {
                    "buySide": {
                        "price": 10,
                        "volume": 100,
                        "priceString": "10.00",
                        "accountId": "must-not-leak",
                    },
                    "sellSide": None,
                    "cookies": ["must-not-leak"],
                }
            ],
            "sessionId": "must-not-leak",
        }
    )

    assert projected == {
        "receivedTime": 123,
        "marketMakerExpected": True,
        "levels": [
            {
                "buySide": {
                    "price": 10,
                    "volume": 100,
                    "priceString": "10.00",
                },
                "sellSide": None,
            }
        ],
    }
    assert "must-not-leak" not in json.dumps(projected)


async def test_registered_tool_is_read_only_bounded_and_uses_broker():
    broker = AuthProcessBroker(mode="one_shot")
    broker.order_depth_snapshot = AsyncMock(
        return_value={
            "orderBookId": ORDER_BOOK_ID,
            "receivedTime": None,
            "levels": [
                {
                    "buyPrice": 10,
                    "buyVolume": 100,
                    "sellPrice": 10.1,
                    "sellVolume": 200,
                }
            ],
            "marketMakerLevelInAsk": None,
            "marketMakerLevelInBid": None,
        }
    )
    try:
        async with Client(create_auth_server(broker)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            tool = tools["get_orderbook_depth"]
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.inputSchema["properties"]["max_levels"]["maximum"] == 50

            result = await client.call_tool(
                "get_orderbook_depth",
                {"order_book_id": ORDER_BOOK_ID, "max_levels": 3},
            )
        assert result.structured_content["orderBookId"] == ORDER_BOOK_ID
        assert result.structured_content["receivedTime"] is None
        broker.order_depth_snapshot.assert_awaited_once_with(ORDER_BOOK_ID, 3)
    finally:
        await broker.aclose()


@pytest.mark.parametrize(
    "error,marker",
    [
        (AuthWorkerRequired(), "AVANZA_AUTH_REQUIRED"),
        (AuthWorkerExpired(), "AVANZA_AUTH_EXPIRED"),
    ],
)
async def test_registered_tool_reports_missing_or_expired_auth(error, marker):
    broker = AuthProcessBroker(mode="one_shot")
    broker.order_depth_snapshot = AsyncMock(side_effect=error)
    try:
        async with Client(create_auth_server(broker)) as client:
            with pytest.raises(ToolError, match=marker):
                await client.call_tool(
                    "get_orderbook_depth",
                    {"order_book_id": ORDER_BOOK_ID},
                )
    finally:
        await broker.aclose()
