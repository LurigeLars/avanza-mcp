"""Regression tests for account-read-backed Avanza health status."""

import json
from unittest.mock import AsyncMock

import pytest

from avanza_mcp.auth import worker
from avanza_mcp.auth.broker import AuthProcessBroker
from avanza_mcp.client.bankid import SessionMaterial


@pytest.mark.parametrize(
    ("result", "expected_state", "expected_code"),
    [
        ({"ok": True, "result": {"accounts": [{"name": "PRIVATE"}]}}, "connected", None),
        ({"ok": False, "code": "worker_error_generic"}, "error", "account_worker_error_generic"),
        ({"ok": False, "code": "read_error_http_503"}, "error", "account_read_error_http_503"),
        ({"ok": False, "code": "auth_expired"}, "disconnected", "auth_expired"),
        ({"ok": False, "code": "not_allowlisted"}, "error", "account_read_failed"),
    ],
)
def test_account_read_health_collapses_to_safe_status(result, expected_state, expected_code):
    status = worker._account_read_health_status(result)
    assert status.state == expected_state
    assert status.error_code == expected_code
    assert "PRIVATE" not in status.model_dump_json()


async def test_persistent_health_probes_accounts_and_does_not_emit_account_data(
    monkeypatch, capsys
):
    session = SessionMaterial((), "private-session-token")

    class Store:
        async def load(self):
            return session

        async def save(self, value):
            raise AssertionError("Unchanged session must not be saved")

        async def delete(self):
            raise AssertionError("Valid session must not be deleted")

    async def validate(saved):
        assert saved is session
        return saved, None

    account = AsyncMock(
        return_value={
            "ok": True,
            "result": {"accounts": [{"account_id": "PRIVATE-ACCOUNT", "balance": 100}]},
        }
    )
    monkeypatch.setattr(worker, "create_session_store", lambda: Store())
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "_account_operation", account)

    await worker._run_once({"action": "health"})
    output = capsys.readouterr().out.strip()
    payload = json.loads(output)
    assert payload == {
        "ok": True,
        "status": {
            "state": "connected",
            "message": "Avanza account access verified with a read-only request.",
            "error_code": None,
        },
    }
    assert "PRIVATE-ACCOUNT" not in output
    assert "private-session-token" not in output
    account.assert_awaited_once()
    assert account.await_args.args[1:] == ("accounts", {})


async def test_persistent_health_fails_closed_when_account_read_fails(
    monkeypatch, capsys
):
    session = SessionMaterial((), "token")

    class Store:
        async def load(self):
            return session

        async def save(self, value):
            raise AssertionError("Unexpected save")

        async def delete(self):
            raise AssertionError("Unexpected delete")

    async def validate(saved):
        return saved, None

    monkeypatch.setattr(worker, "create_session_store", lambda: Store())
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(
        worker, "_account_operation",
        AsyncMock(return_value={"ok": False, "code": "worker_error_generic"}),
    )

    await worker._run_once({"action": "health"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"]["state"] == "error"
    assert payload["status"]["error_code"] == "account_worker_error_generic"


async def test_persistent_health_fails_closed_when_validation_is_inconclusive(
    monkeypatch, capsys
):
    session = SessionMaterial((), "token")

    class Store:
        async def load(self):
            return session

        async def save(self, value):
            raise AssertionError("Unexpected save")

        async def delete(self):
            raise AssertionError("Unexpected delete")

    async def validate(saved):
        return None, "network"

    monkeypatch.setattr(worker, "create_session_store", lambda: Store())
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(
        worker, "_account_operation",
        AsyncMock(return_value={"ok": True, "result": {"accounts": []}}),
    )

    await worker._run_once({"action": "health"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"]["state"] == "error"
    assert payload["status"]["error_code"] == "session_validation_failed"


async def test_broker_persistent_health_is_one_isolated_probe(monkeypatch):
    broker = AuthProcessBroker(mode="persistent")
    response = {
        "ok": True,
        "status": {
            "state": "connected",
            "message": "Account read verified",
            "error_code": None,
        },
    }
    run_once = AsyncMock(return_value=response)
    stop_market = AsyncMock()
    monkeypatch.setattr(broker, "_run_once", run_once)
    monkeypatch.setattr(broker, "_stop_market_daemon", stop_market)

    try:
        status = await broker.health_status()
        assert status.state == "connected"
        run_once.assert_awaited_once_with({"action": "health"})
        stop_market.assert_awaited_once()
    finally:
        await broker.aclose()


async def test_broker_one_shot_health_does_not_call_account_operation(monkeypatch):
    broker = AuthProcessBroker(mode="one_shot")

    class LiveProcess:
        returncode = None

    broker._daemon = LiveProcess()
    cmd = AsyncMock(return_value={
        "ok": True,
        "status": {
            "state": "connected",
            "message": "Account read verified",
            "error_code": None,
        },
    })
    monkeypatch.setattr(broker, "_command", cmd)
    try:
        status = await broker.health_status()
        assert status.state == "connected"
        assert cmd.await_count == 1
        assert cmd.await_args.args[1] == {"action": "health"}
    finally:
        broker._daemon = None
        await broker.aclose()


async def test_one_shot_daemon_health_only_returns_status_and_preserves_session(
    monkeypatch, capsys
):
    session = SessionMaterial((), "PRIVATE-SESSION")
    account = AsyncMock(
        return_value={"ok": True, "result": {"accounts": [{"id": "PRIVATE-ACCOUNT"}]}}
    )

    class FakeAuth:
        def __init__(self, *args, **kwargs):
            self.session = None

        async def open_browser(self):
            self.session = session
            return worker.AuthStatus(state="connected", message="connected")

        def status(self):
            state = "connected" if self.session is not None else "disconnected"
            return worker.AuthStatus(state=state, message=state)

        async def disconnect(self):
            self.session = None
            return worker.AuthStatus(state="disconnected", message="disconnected")

        async def aclose(self):
            pass

    def feed(_loop, queue):
        for action in ("connect", "health", "status", "shutdown"):
            queue.put_nowait(json.dumps({"action": action}))

    monkeypatch.setattr(worker, "BrowserAuth", FakeAuth)
    monkeypatch.setattr(worker, "_account_operation", account)
    monkeypatch.setattr(worker, "_start_stdin_reader", feed)
    monkeypatch.setattr(worker, "_parent_alive", lambda _pid: True)

    await worker._run_daemon("one_shot", 123)
    output = capsys.readouterr().out
    statuses = [json.loads(line)["status"]["state"] for line in output.splitlines()]
    assert statuses == ["connected", "connected", "connected", "disconnected"]
    assert "PRIVATE-ACCOUNT" not in output
    assert "PRIVATE-SESSION" not in output
    account.assert_awaited_once()
    assert account.await_args.args[1:] == ("accounts", {})
