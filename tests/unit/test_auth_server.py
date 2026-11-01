"""Authenticated server composition using credential-isolated workers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import avanza_mcp
from avanza_mcp.auth.broker import AuthWorkerOperationError, AuthWorkerRequired
from avanza_mcp.auth.browser import AuthStatus
from avanza_mcp.auth.server import create_auth_server, run_auth_server


class FakeBroker:
    def __init__(self):
        self.closed = False
        self.opened = 0
        self.warmed = 0
        self.account_calls = []
        self.market_calls = []
        self.market_batch_calls = []
        self.market_response = None
        self.market_batch_response = None
        self.account_results = {}
        self.forum_calls = []
        self.forum_result = {
            "post_id": "post-1",
            "instrument_name": "Synthetic",
            "instrument_slug": "synthetic",
            "company_name": "Synthetic AB",
            "company_slug": "synthetic-ab",
        }

    async def warm_market_worker(self):
        self.warmed += 1

    async def connect(self):
        self.opened += 1
        return AuthStatus(
            state="awaiting_approval", message="Approve in the local browser."
        )

    async def disconnect(self):
        self.opened += 1
        return AuthStatus(state="disconnected", message="Avanza is not connected.")

    async def status(self):
        return AuthStatus(state="disconnected", message="Avanza is not connected.")

    async def health_status(self):
        return await self.status()

    async def connect_forum(self):
        self.opened += 1
        return AuthStatus(
            state="awaiting_approval",
            message="Approve Placera Forum sign-in in the local browser window.",
        )

    async def disconnect_forum(self):
        self.opened += 1
        return AuthStatus(
            state="disconnected", message="Placera Forum is not connected."
        )

    async def forum_status(self):
        return AuthStatus(
            state="disconnected", message="Placera Forum is not connected."
        )

    async def forum_post(self, arguments):
        self.forum_calls.append(arguments)
        return self.forum_result

    async def account(self, operation, arguments):
        self.account_calls.append((operation, arguments))
        if operation not in self.account_results:
            raise AuthWorkerRequired
        return self.account_results[operation]

    async def market_request(self, method, path, kwargs):
        self.market_calls.append((method, path, kwargs))
        return self.market_response

    async def market_data_batch(self, paths):
        self.market_batch_calls.append(list(paths))
        return self.market_batch_response

    async def aclose(self):
        self.closed = True


async def test_auth_server_mounts_public_contract_and_adds_auth_tools():
    broker = FakeBroker()
    server = create_auth_server(broker)  # type: ignore[arg-type]
    async with Client(server) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert len(tools) == 60
        assert {
            "connect_avanza",
            "disconnect_avanza",
            "get_auth_status",
            "get_execution_quote",
            "get_accounts",
            "get_holdings",
            "get_transactions",
            "get_watchlists",
            "get_price_alerts",
            "get_portfolio_insights",
            "get_portfolio_snapshot",
            "get_instrument_news",
            "get_instrument_news_batch",
            "get_forum_posts",
            "connect_forum",
            "disconnect_forum",
            "get_forum_auth_status",
            "create_forum_post",
            "get_insider_transactions",
            "get_active_orders",
            "get_deals",
            "get_stop_loss_orders",
        } <= tools
        assert {"get_credit_info", "get_current_offers", "enrich_leveraged_snapshot", "get_orderbook_depth"}.isdisjoint(tools)
        assert len(await client.list_prompts()) == 3

        connected = await client.call_tool("connect_avanza", {})
        assert connected.structured_content["state"] == "awaiting_approval"
        status = await client.call_tool("get_auth_status", {})
        assert status.structured_content == {
            "state": "disconnected",
            "message": "Avanza is not connected.",
            "error_code": None,
        }
        with pytest.raises(ToolError, match="AVANZA_AUTH_REQUIRED"):
            await client.call_tool("get_accounts", {})

    assert broker.closed
    assert broker.opened == 1
    assert broker.warmed == 1
    assert broker.account_calls == [("accounts", {})]


async def test_existing_market_tools_delegate_authenticated_request_without_session_material():
    broker = FakeBroker()
    broker.market_response = httpx.Response(
        200, json={"last": 10, "isRealTime": True, "unknownSensitiveField": "discard"}
    )

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        result = await client.call_tool("get_stock_quote", {"order_book_id": "123"})

    assert result.structured_content["last"] == 10
    assert result.structured_content["isRealTime"] is True
    assert len(broker.market_calls) == 1
    method, path, kwargs = broker.market_calls[0]
    assert method == "GET"
    assert path == "/_api/market-guide/stock/123/quote"
    assert kwargs.get("params") is None


async def test_execution_quote_uses_trading_critical_projection_and_stays_compact():
    broker = FakeBroker()
    broker.market_response = httpx.Response(
        200,
        json={
            "quote": {
                "buy": 227.10,
                "sell": 227.20,
                "last": 226.90,
                "updated": "2026-09-30T12:30:01.000",
                "timeOfLast": "2026-09-30T12:29:59.000",
                "isRealTime": True,
                "highest": 999,
                "accountId": "must-not-leak",
            },
            "orderDepth": {"accountId": "must-not-leak"},
            "trades": [{"accountId": "must-not-leak"}],
        },
    )

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "get_execution_quote", {"order_book_id": "4478"}
        )

    assert result.structured_content == {
        "orderBookId": "4478",
        "source": "authenticated_trading_critical",
        "buy": 227.10,
        "sell": 227.20,
        "last": 226.90,
        "updated": "2026-09-30T12:30:01.000",
        "timeOfLast": "2026-09-30T12:29:59.000",
        "isRealTime": True,
    }
    assert "must-not-leak" not in str(result.structured_content)
    assert broker.market_calls == [
        (
            "GET",
            "/_api/trading-critical/rest/marketdata/4478",
            {"params": None, "json": None},
        )
    ]


async def test_auth_lifespan_resets_global_worker_delegate():
    broker = FakeBroker()
    server = create_auth_server(broker)  # type: ignore[arg-type]

    async with Client(server):
        assert avanza_mcp._auth_request_delegate is not None

    assert broker.closed
    assert avanza_mcp._auth_session_provider is None
    assert avanza_mcp._auth_session_invalidator is None
    assert avanza_mcp._auth_request_delegate is None
    assert avanza_mcp._auth_market_data_batch_delegate is None


def test_auth_transport_is_stdio(monkeypatch):
    server = Mock()
    create = Mock(return_value=server)
    monkeypatch.setattr("avanza_mcp.auth.server.create_auth_server", create)
    run_auth_server()
    create.assert_called_once_with()
    server.run.assert_called_once_with()


def test_cli_uses_authenticated_server_by_default(monkeypatch):
    run = Mock()
    monkeypatch.setattr("avanza_mcp.auth.server.run_auth_server", run)
    avanza_mcp.main()
    run.assert_called_once_with()


async def test_account_tool_surfaces_only_safe_diagnostic_class():
    class DiagnosticBroker(FakeBroker):
        async def account(self, operation, arguments):
            raise AuthWorkerOperationError("read_error_http_404")

    async with Client(create_auth_server(DiagnosticBroker())) as client:  # type: ignore[arg-type]
        with pytest.raises(ToolError, match=r"Safe diagnostic: http_404"):
            await client.call_tool("get_watchlists", {})


async def test_account_tool_does_not_surface_unclassified_worker_text():
    class UnsafeTextBroker(FakeBroker):
        async def account(self, operation, arguments):
            raise AuthWorkerOperationError("secret=must-not-leak")

    async with Client(create_auth_server(UnsafeTextBroker())) as client:  # type: ignore[arg-type]
        with pytest.raises(ToolError) as exc:
            await client.call_tool("get_watchlists", {})

    assert "must-not-leak" not in str(exc.value)


async def test_account_tool_surfaces_only_safe_exception_class():
    class DiagnosticBroker(FakeBroker):
        async def account(self, operation, arguments):
            raise AuthWorkerOperationError("worker_error_AttributeError")

    async with Client(create_auth_server(DiagnosticBroker())) as client:  # type: ignore[arg-type]
        with pytest.raises(ToolError, match=r"Safe diagnostic: exception_AttributeError"):
            await client.call_tool("get_watchlists", {})


async def test_account_tool_surfaces_safe_session_validation_code():
    class DiagnosticBroker(FakeBroker):
        async def account(self, operation, arguments):
            raise AuthWorkerOperationError("network_http_503")

    async with Client(create_auth_server(DiagnosticBroker())) as client:  # type: ignore[arg-type]
        with pytest.raises(
            ToolError, match=r"Safe diagnostic: session_validation_network_http_503"
        ):
            await client.call_tool("get_watchlists", {})


async def test_account_tool_surfaces_safe_server_validation_type():
    class ValidationBroker(FakeBroker):
        async def account(self, operation, arguments):
            return {"watchlists": [{"name": "Missing ID", "order_book_ids": []}]}

    async with Client(create_auth_server(ValidationBroker())) as client:  # type: ignore[arg-type]
        with pytest.raises(
            ToolError, match=r"Safe diagnostic: server_validation_missing"
        ):
            await client.call_tool("get_watchlists", {})


@pytest.mark.parametrize(
    ("worker_error", "diagnostic"),
    [
        ("Auth worker closed unexpectedly", "broker_closed"),
        ("Auth worker returned invalid data", "broker_invalid_data"),
        ("Auth worker returned unsafe data", "broker_unsafe_data"),
        ("worker_error", "worker_error_generic"),
    ],
)
async def test_account_tool_surfaces_only_fixed_broker_diagnostics(
    worker_error, diagnostic
):
    class DiagnosticBroker(FakeBroker):
        async def account(self, operation, arguments):
            raise AuthWorkerOperationError(worker_error)

    async with Client(create_auth_server(DiagnosticBroker())) as client:  # type: ignore[arg-type]
        with pytest.raises(ToolError, match=rf"Safe diagnostic: {diagnostic}"):
            await client.call_tool("get_watchlists", {})


async def test_instrument_news_batch_tool_delegates_once_and_preserves_partial_coverage():
    broker = FakeBroker()
    broker.account_results["instrument_news_batch"] = {
        "items": [
            {
                "order_book_id": "123",
                "articles": [
                    {
                        "published_at": "2026-09-30T12:00:00",
                        "headline": "News",
                        "summary": None,
                        "source": "Source",
                        "category": None,
                        "url": "/article",
                    }
                ],
                "truncated": False,
            }
        ],
        "failed_order_book_ids": ["456"],
    }

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "get_instrument_news_batch",
            {"order_book_ids": ["123", "456"], "limit_per_instrument": 5},
        )

    assert result.structured_content == broker.account_results["instrument_news_batch"]
    assert broker.account_calls == [
        (
            "instrument_news_batch",
            {"order_book_ids": ["123", "456"], "limit_per_instrument": 5},
        )
    ]


async def test_transactions_tool_forwards_optional_filters_without_new_tool():
    broker = FakeBroker()
    broker.account_results["transactions"] = {
        "transactions": [],
        "returned": 0,
        "total_reported": 0,
        "truncated": False,
        "first_transaction_date": None,
    }

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        tools = {tool.name for tool in await client.list_tools()}
        assert len(tools) == 60
        result = await client.call_tool(
            "get_transactions",
            {
                "from_date": "2026-01-01",
                "to_date": "2026-01-31",
                "limit": 25,
                "isin": "SE0000115446",
                "transaction_types": ["BUY", "SELL"],
            },
        )

    assert result.structured_content == broker.account_results["transactions"]
    assert broker.account_calls == [
        (
            "transactions",
            {
                "from_date": "2026-01-01",
                "to_date": "2026-01-31",
                "limit": 25,
                "isin": "SE0000115446",
                "transaction_types": ["BUY", "SELL"],
            },
        )
    ]


async def test_portfolio_snapshot_tool_is_bounded_and_delegates_once():
    broker = FakeBroker()
    broker.account_results["portfolio_snapshot"] = {
        "accounts": [],
        "holdings": [],
        "cash_positions": [],
        "active_orders": [],
        "active_orders_truncated": False,
        "deals": [],
        "deals_truncated": False,
        "stop_loss_orders": [],
        "stop_loss_orders_truncated": False,
    }

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        result = await client.call_tool("get_portfolio_snapshot", {"limit": 25})

    assert result.structured_content == broker.account_results["portfolio_snapshot"]
    assert broker.account_calls == [("portfolio_snapshot", {"limit": 25})]


async def test_forum_posts_tool_delegates_bounded_read_once():
    broker = FakeBroker()
    broker.account_results["forum_posts"] = {
        "posts": [
            {
                "author": "Synthetic user",
                "title": "Synthetic title",
                "content": "Synthetic discussion",
                "likes": 1,
                "replies": 2,
                "timestamp": 3,
                "url": "/forum/post",
            }
        ],
        "truncated": False,
    }

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "get_forum_posts", {"order_book_id": "123", "limit": 7}
        )

    assert result.structured_content == broker.account_results["forum_posts"]
    assert broker.account_calls == [
        ("forum_posts", {"order_book_id": "123", "limit": 7})
    ]


async def test_forum_connection_tools_are_separate_from_avanza_auth():
    broker = FakeBroker()

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        connected = await client.call_tool("connect_forum", {})
        assert connected.structured_content["state"] == "awaiting_approval"
        status = await client.call_tool("get_forum_auth_status", {})
        assert status.structured_content == {
            "state": "disconnected",
            "message": "Placera Forum is not connected.",
            "error_code": None,
        }
        disconnected = await client.call_tool("disconnect_forum", {})
        assert disconnected.structured_content["state"] == "disconnected"

    assert broker.opened == 2


async def test_create_forum_post_requires_confirmation_before_any_write(monkeypatch):
    broker = FakeBroker()
    stock = SimpleNamespace(isin="SE0000115446")
    get_stock = AsyncMock(return_value=stock)
    monkeypatch.setattr(
        "avanza_mcp.auth.server.MarketDataService.get_stock_info",
        get_stock,
    )

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        with pytest.raises(ToolError, match="CONFIRMATION_REQUIRED"):
            await client.call_tool(
                "create_forum_post",
                {
                    "order_book_id": "5269",
                    "title": "Title",
                    "content": "Exact body",
                    "confirm": False,
                },
            )

    get_stock.assert_not_awaited()
    assert broker.forum_calls == []


async def test_create_forum_post_maps_order_book_id_to_isin_and_delegates_once(monkeypatch):
    broker = FakeBroker()
    stock = SimpleNamespace(isin="SE0000115446")
    get_stock = AsyncMock(return_value=stock)
    monkeypatch.setattr(
        "avanza_mcp.auth.server.MarketDataService.get_stock_info",
        get_stock,
    )

    async with Client(create_auth_server(broker)) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "create_forum_post",
            {
                "order_book_id": "5269",
                "title": "Title",
                "content": "Exact body",
                "confirm": True,
            },
        )

    assert result.structured_content == broker.forum_result
    get_stock.assert_awaited_once_with("5269")
    assert broker.forum_calls == [
        {
            "isin": "SE0000115446",
            "title": "Title",
            "content": "Exact body",
            "confirm": True,
        }
    ]
