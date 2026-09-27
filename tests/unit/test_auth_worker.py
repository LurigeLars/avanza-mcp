"""Persistent auth-worker concurrency and fail-closed tests."""

import asyncio
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
