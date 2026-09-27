"""Authenticated server composition using credential-isolated workers."""

import os
import subprocess
import sys
from unittest.mock import Mock

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
        self.account_calls = []
        self.market_calls = []
        self.market_response = None
        self.account_results = {}

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

    async def account(self, operation, arguments):
        self.account_calls.append((operation, arguments))
        if operation not in self.account_results:
            raise AuthWorkerRequired
        return self.account_results[operation]

    async def market_request(self, method, path, kwargs):
        self.market_calls.append((method, path, kwargs))
        return self.market_response

    async def aclose(self):
        self.closed = True


async def test_auth_server_mounts_public_contract_and_adds_auth_tools():
    broker = FakeBroker()
    server = create_auth_server(broker)  # type: ignore[arg-type]
    async with Client(server) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert len(tools) == 51
        assert {
            "connect_avanza",
            "disconnect_avanza",
            "get_auth_status",
            "get_accounts",
            "get_holdings",
            "get_transactions",
            "get_watchlists",
            "get_price_alerts",
            "get_portfolio_insights",
            "get_instrument_news",
            "get_insider_transactions",
            "get_active_orders",
            "get_deals",
            "get_stop_loss_orders",
        } <= tools
        assert {"get_credit_info", "get_current_offers", "get_forum_posts"}.isdisjoint(tools)
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


async def test_auth_lifespan_resets_global_worker_delegate():
    broker = FakeBroker()
    server = create_auth_server(broker)  # type: ignore[arg-type]

    async with Client(server):
        assert avanza_mcp._auth_request_delegate is not None

    assert broker.closed
    assert avanza_mcp._auth_session_provider is None
    assert avanza_mcp._auth_session_invalidator is None
    assert avanza_mcp._auth_request_delegate is None


def test_auth_transport_is_stdio(monkeypatch):
    server = Mock()
    create = Mock(return_value=server)
    monkeypatch.setattr("avanza_mcp.auth.server.create_auth_server", create)
    run_auth_server()
    create.assert_called_once_with()
    server.run.assert_called_once_with()


def test_cli_preserves_public_default(monkeypatch):
    run = Mock()
    monkeypatch.delenv("AVANZA_MCP_AUTH", raising=False)
    monkeypatch.setattr("avanza_mcp.mcp.run", run)
    avanza_mcp.main()
    run.assert_called_once_with()


def test_cli_selects_auth_stdio_from_environment(monkeypatch):
    run = Mock()
    monkeypatch.setenv("AVANZA_MCP_AUTH", "1")
    monkeypatch.setattr("avanza_mcp.auth.server.run_auth_server", run)
    avanza_mcp.main()
    run.assert_called_once_with()


def test_cli_rejects_ambiguous_auth_environment(monkeypatch):
    monkeypatch.setenv("AVANZA_MCP_AUTH", "true")
    with pytest.raises(SystemExit):
        avanza_mcp.main()


def test_public_startup_does_not_import_account_modules():
    script = """
import sys
from avanza_mcp import main, mcp
mcp.run = lambda: None
main()
assert 'avanza_mcp.auth.server' not in sys.modules
"""
    env = os.environ.copy()
    env.pop("AVANZA_MCP_AUTH", None)
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


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
