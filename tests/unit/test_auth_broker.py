"""Policy tests for the credential-isolating auth broker."""

import json

import pytest
from unittest.mock import AsyncMock

from avanza_mcp.auth.broker import (
    AuthProcessBroker,
    AuthWorkerOperationError,
    _contains_forbidden_key,
    session_mode_from_environment,
)


def test_session_mode_defaults_to_persistent(monkeypatch):
    monkeypatch.delenv("AVANZA_SESSION_MODE", raising=False)
    assert session_mode_from_environment() == "persistent"


@pytest.mark.parametrize("mode", ["persistent", "memory_only", "one_shot"])
def test_session_modes_are_explicit(monkeypatch, mode):
    monkeypatch.setenv("AVANZA_SESSION_MODE", mode)
    assert session_mode_from_environment() == mode


def test_invalid_session_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("AVANZA_SESSION_MODE", "forever")
    with pytest.raises(RuntimeError, match="AVANZA_SESSION_MODE"):
        session_mode_from_environment()


def test_worker_result_secret_keys_are_rejected():
    assert _contains_forbidden_key({"result": {"securityToken": "sentinel"}})
    assert _contains_forbidden_key({"result": [{"cookies": []}]})
    assert _contains_forbidden_key({"result": {"sessionId": "sentinel"}})
    assert not _contains_forbidden_key({"result": {"account_id": "123"}})

    with pytest.raises(AuthWorkerOperationError, match="unsafe"):
        AuthProcessBroker._decode_response(
            json.dumps({"ok": True, "result": {"security_token": "sentinel"}}).encode()
        )


async def test_one_shot_mode_reuses_live_session_for_approved_market_calls(monkeypatch):
    broker = AuthProcessBroker(mode="one_shot")
    process = _LiveProcess()
    broker._daemon = process  # type: ignore[assignment]
    command = AsyncMock(
        side_effect=[
            {
                "ok": True,
                "status": {
                    "state": "connected",
                    "message": "Avanza is connected for this MCP process.",
                    "error_code": None,
                },
            },
            {"ok": True, "result": {"last": 10}},
        ]
    )
    monkeypatch.setattr(broker, "_command", command)
    try:
        response = await broker.market_request(
            "GET",
            "/_api/market-guide/stock/123/quote",
            {"params": None},
        )
        assert response is not None
        assert response.status_code == 200
        assert response.json() == {"last": 10}
        assert command.await_count == 2
        sent = command.await_args_list[1].args[1]
        assert sent["action"] == "market"
        assert sent["path"] == "/_api/market-guide/stock/123/quote"
    finally:
        broker._daemon = None
        await broker.aclose()


async def test_market_broker_rejects_non_allowlisted_authenticated_path(monkeypatch):
    broker = AuthProcessBroker(mode="persistent")
    command = AsyncMock()
    monkeypatch.setattr(broker, "_run_once", command)
    try:
        response = await broker.market_request(
            "POST",
            "/_api/trading/rest/orders",
            {"json": {"side": "BUY"}},
        )
        assert response is None
        command.assert_not_awaited()
    finally:
        await broker.aclose()


class _LiveProcess:
    returncode = None


async def test_persistent_disconnect_blocks_new_authenticated_operations():
    broker = AuthProcessBroker(mode="persistent")
    broker._ui_action = "disconnect"
    broker._ui_process = _LiveProcess()  # type: ignore[assignment]
    try:
        with pytest.raises(AuthWorkerOperationError, match="Disconnect"):
            await broker.account("accounts", {})

        response = await broker.market_request(
            "GET",
            "/_api/market-guide/stock/123/quote",
            {"params": None},
        )
        assert response is None
    finally:
        broker._ui_process = None
        broker._ui_action = None
        await broker.aclose()


def test_worker_account_allowlist_excludes_hidden_internal_operations():
    from avanza_mcp.auth.worker import _ALLOWED_ACCOUNT_OPERATIONS

    assert {
        "credit_info",
        "current_offers",
        "forum_posts",
    }.isdisjoint(_ALLOWED_ACCOUNT_OPERATIONS)
    assert _ALLOWED_ACCOUNT_OPERATIONS == {
        "accounts",
        "holdings",
        "transactions",
        "watchlists",
        "price_alerts",
        "portfolio_insights",
        "instrument_news",
        "insider_transactions",
        "active_orders",
        "deals",
        "stop_loss_orders",
    }


def test_worker_argument_validators_fail_closed():
    from avanza_mcp.auth.worker import _bounded_int, _numeric_order_book_id, _only_arguments

    assert _bounded_int(None, default=20, minimum=1, maximum=100) == 20
    assert _numeric_order_book_id("4478") == "4478"
    _only_arguments({"limit": 20}, {"limit"})

    for value in (0, 101, True):
        with pytest.raises((TypeError, ValueError)):
            _bounded_int(value, default=20, minimum=1, maximum=100)

    for value in ("", "12x", "１２３"):
        with pytest.raises(ValueError):
            _numeric_order_book_id(value)

    with pytest.raises(ValueError):
        _only_arguments({"limit": 20, "extra": "blocked"}, {"limit"})



def test_worker_blocks_credential_shaped_market_payload_before_ipc():
    from avanza_mcp.auth.worker import _contains_forbidden_market_result

    assert _contains_forbidden_market_result({"securityToken": "secret"})
    assert _contains_forbidden_market_result({"nested": {"sessionId": "secret"}})
    assert _contains_forbidden_market_result({"authorization": "Bearer secret"})
    assert not _contains_forbidden_market_result(
        {"last": 10, "isRealTime": True, "bid": 9.9, "ask": 10.1}
    )
