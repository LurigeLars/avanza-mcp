"""Persistent auth-worker concurrency and fail-closed tests."""

import asyncio
import io
import json

from avanza_mcp.auth import worker
from avanza_mcp.client.bankid import SessionMaterial


class FakeStore:
    def __init__(self, session):
        self.session = session
        self.loads = 0
        self.saves = 0
        self.deletes = 0

    async def load(self):
        self.loads += 1
        return self.session

    async def save(self, session):
        self.saves += 1
        self.session = session

    async def delete(self):
        self.deletes += 1
        self.session = None


async def test_persistent_operation_validates_and_reads_concurrently(monkeypatch, capsys):
    session = SessionMaterial((), "token")
    store = FakeStore(session)
    validation_started = asyncio.Event()
    operation_started = asyncio.Event()

    async def validate(saved):
        assert saved is session
        validation_started.set()
        await operation_started.wait()
        return saved, None

    async def account(auth, operation, arguments):
        assert auth.session is session
        assert operation == "accounts"
        assert arguments == {}
        operation_started.set()
        await validation_started.wait()
        return {"ok": True, "result": {"accounts": []}}

    monkeypatch.setattr(worker, "create_session_store", lambda: store)
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "_account_operation", account)

    await worker._run_once(
        {"action": "account", "operation": "accounts", "arguments": {}}
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "result": {"accounts": []}}
    assert validation_started.is_set()
    assert operation_started.is_set()
    assert store.loads == 1
    assert store.saves == 0
    assert store.deletes == 0


async def test_market_batch_validates_session_once_for_multiple_paths(
    monkeypatch, capsys
):
    session = SessionMaterial((), "token")
    store = FakeStore(session)
    validation_calls = 0
    batch_calls = 0

    async def validate(saved):
        nonlocal validation_calls
        validation_calls += 1
        assert saved is session
        return saved, None

    async def batch(auth, command):
        nonlocal batch_calls
        batch_calls += 1
        assert auth.session is session
        assert command["paths"] == [
            "/_api/trading-critical/rest/marketdata/101",
            "/_api/trading-critical/rest/marketdata/102",
        ]
        return {
            "ok": True,
            "result": [
                {"quote": {"buy": 10.0}},
                {"quote": {"buy": 20.0}},
            ],
        }

    monkeypatch.setattr(worker, "create_session_store", lambda: store)
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "_market_batch_operation", batch)

    await worker._run_once(
        {
            "action": "market_batch",
            "paths": [
                "/_api/trading-critical/rest/marketdata/101",
                "/_api/trading-critical/rest/marketdata/102",
            ],
        }
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["result"]) == 2
    assert validation_calls == 1
    assert batch_calls == 1
    assert store.loads == 1
    assert store.saves == 0



async def test_market_batch_fetches_with_bounded_concurrency_and_preserves_order(monkeypatch):
    active = 0
    max_active = 0

    class FakeResponse:
        def __init__(self, value):
            self.status_code = 200
            self._value = value

        def json(self):
            return {"quote": {"buy": self._value, "sell": self._value + 0.1}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request_authenticated(self, method, path):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.01)
                value = int(path.rsplit("/", 1)[-1])
                return FakeResponse(value)
            finally:
                active -= 1

    monkeypatch.setattr(worker, "AvanzaClient", FakeClient)
    auth = worker._RequestAuth(SessionMaterial((), "token"))
    paths = [
        f"/_api/trading-critical/rest/marketdata/{index}" for index in range(1, 25)
    ]
    result = await worker._market_batch_operation(auth, {"paths": paths})

    assert result["ok"] is True
    assert [item["quote"]["buy"] for item in result["result"]] == list(range(1, 25))
    assert 1 < max_active <= worker._MARKET_BATCH_CONCURRENCY


async def test_invalid_validation_discards_concurrent_success(monkeypatch, capsys):
    session = SessionMaterial((), "token")
    store = FakeStore(session)

    async def validate(saved):
        return None, None

    async def market(auth, command):
        return {"ok": True, "result": {"last": 10, "isRealTime": True}}

    monkeypatch.setattr(worker, "create_session_store", lambda: store)
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "_market_operation", market)

    await worker._run_once(
        {
            "action": "market",
            "method": "GET",
            "path": "/_api/market-guide/stock/4478/quote",
            "params": None,
            "json": None,
        }
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "code": "auth_expired"}
    assert store.deletes == 1


async def test_validation_failure_never_returns_operation_result(monkeypatch, capsys):
    session = SessionMaterial((), "token")
    store = FakeStore(session)

    async def validate(saved):
        return None, "network_http_503"

    async def account(auth, operation, arguments):
        return {"ok": True, "result": {"accounts": [{"account_id": "secret"}]}}

    monkeypatch.setattr(worker, "create_session_store", lambda: store)
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "_account_operation", account)

    await worker._run_once(
        {"action": "account", "operation": "accounts", "arguments": {}}
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "code": "network_http_503"}
    assert store.deletes == 0
    assert store.saves == 0


async def test_refreshed_session_is_saved_only_when_changed(monkeypatch, capsys):
    saved = SessionMaterial((), "old-token")
    refreshed = SessionMaterial((), "new-token")
    store = FakeStore(saved)

    async def validate(session):
        return refreshed, None

    async def account(auth, operation, arguments):
        return {"ok": True, "result": {"accounts": []}}

    monkeypatch.setattr(worker, "create_session_store", lambda: store)
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "_account_operation", account)

    await worker._run_once(
        {"action": "account", "operation": "accounts", "arguments": {}}
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert store.saves == 1
    assert store.session is refreshed


async def test_auth_expired_operation_retries_once_with_refreshed_session(
    monkeypatch, capsys
):
    saved = SessionMaterial((), "old-token")
    refreshed = SessionMaterial((), "new-token")
    store = FakeStore(saved)
    seen = []

    async def validate(session):
        return refreshed, None

    async def account(auth, operation, arguments):
        seen.append(auth.session._security_token)
        if len(seen) == 1:
            await auth.invalidate_session()
            return {"ok": False, "code": "auth_expired"}
        return {"ok": True, "result": {"accounts": []}}

    monkeypatch.setattr(worker, "create_session_store", lambda: store)
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "_account_operation", account)

    await worker._run_once(
        {"action": "account", "operation": "accounts", "arguments": {}}
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert seen == ["old-token", "new-token"]
    assert store.saves == 1
    assert store.deletes == 0


def test_account_read_failure_codes_are_safely_bounded():
    from avanza_mcp.auth.worker import _safe_account_read_failure_code
    from avanza_mcp.client.accounts import AccountReadError

    assert _safe_account_read_failure_code(AccountReadError("http_404")) == "read_error_http_404"
    assert _safe_account_read_failure_code(AccountReadError("network")) == "read_error_network"
    assert _safe_account_read_failure_code(AccountReadError("invalid_json")) == "read_error_invalid_json"
    assert _safe_account_read_failure_code(AccountReadError("secret=must-not-leak")) == "read_error_response_shape"


def test_unexpected_account_failure_exposes_only_exception_class():
    class SyntheticFailure(Exception):
        pass

    error = SyntheticFailure("secret=must-not-leak")
    name = type(error).__name__
    assert name == "SyntheticFailure"
    assert name.isidentifier()
    assert "must-not-leak" not in f"worker_error_{name}"


def test_emit_uses_dedicated_protocol_stream(monkeypatch):
    protocol = io.StringIO()
    ordinary = io.StringIO()
    monkeypatch.setattr(worker, "_PROTOCOL_STDOUT", protocol)
    monkeypatch.setattr(worker.sys, "stdout", ordinary)

    worker._emit({"ok": True, "result": {"value": 1}})

    assert ordinary.getvalue() == ""
    assert json.loads(protocol.getvalue()) == {"ok": True, "result": {"value": 1}}
