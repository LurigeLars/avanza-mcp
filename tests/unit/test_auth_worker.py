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


async def test_persistent_market_daemon_reuses_validation_and_client(
    monkeypatch, capsys
):
    session = SessionMaterial((), "token")
    store = FakeStore(session)
    validation_calls = 0
    client_instances = []
    seen_clients = []

    async def validate(saved):
        nonlocal validation_calls
        validation_calls += 1
        assert saved is session
        return saved, None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            client_instances.append(self)
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    async def market(auth, command, client=None):
        assert auth.session is session
        assert client is not None
        seen_clients.append(client)
        return {"ok": True, "result": {"last": len(seen_clients)}}

    def feed(loop, queue):
        for payload in (
            {"action": "warm"},
            {
                "action": "market",
                "method": "GET",
                "path": "/_api/market-guide/stock/4478/quote",
                "params": None,
                "json": None,
            },
            {
                "action": "market",
                "method": "GET",
                "path": "/_api/market-guide/stock/4478/quote",
                "params": None,
                "json": None,
            },
            {"action": "shutdown"},
        ):
            loop.call_soon(queue.put_nowait, json.dumps(payload))

    monkeypatch.setattr(worker, "create_session_store", lambda: store)
    monkeypatch.setattr(worker, "_validate_saved_session", validate)
    monkeypatch.setattr(worker, "AvanzaClient", FakeClient)
    monkeypatch.setattr(worker, "_market_operation", market)
    monkeypatch.setattr(worker, "_start_stdin_reader", feed)
    monkeypatch.setattr(worker, "_parent_alive", lambda _pid: True)

    await worker._run_persistent_market_daemon(123)

    payloads = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert payloads == [
        {"ok": True},
        {"ok": True, "result": {"last": 1}},
        {"ok": True, "result": {"last": 2}},
        {"ok": True},
    ]
    assert validation_calls == 1
    assert store.loads == 1
    assert len(client_instances) == 1
    assert seen_clients == [client_instances[0], client_instances[0]]
    assert client_instances[0].kwargs["max_connections"] == (
        worker._MARKET_BATCH_CONCURRENCY
    )
    assert client_instances[0].kwargs["min_request_interval"] == (
        worker._MARKET_BATCH_MIN_REQUEST_INTERVAL
    )


def test_persistent_market_session_revalidation_is_bounded():
    assert worker._MARKET_SESSION_REVALIDATE_SECONDS == 15 * 60


def test_memory_only_session_policy_is_2h_idle_and_16h_absolute():
    assert worker._MEMORY_ONLY_IDLE_SECONDS == 2 * 60 * 60
    assert worker._MEMORY_ONLY_ABSOLUTE_SECONDS == 16 * 60 * 60


def test_memory_only_idle_timeout_slides_with_recent_activity():
    assert not worker._daemon_session_expired(
        mode="memory_only",
        now=120 * 60,
        last_activity=30 * 60,
        session_started_at=0,
    )
    assert worker._daemon_session_expired(
        mode="memory_only",
        now=150 * 60,
        last_activity=30 * 60,
        session_started_at=0,
    )


def test_memory_only_absolute_timeout_never_slides_with_activity():
    assert worker._daemon_session_expired(
        mode="memory_only",
        now=16 * 60 * 60,
        last_activity=(16 * 60 * 60) - 1,
        session_started_at=0,
    )


def test_one_shot_does_not_inherit_memory_only_absolute_timeout():
    assert not worker._daemon_session_expired(
        mode="one_shot",
        now=16 * 60 * 60,
        last_activity=(16 * 60 * 60) - 1,
        session_started_at=0,
    )


async def test_order_depth_does_not_extend_memory_only_idle_timer(monkeypatch, capsys):
    session = SessionMaterial((), "token")
    clock = {"now": 0.0}
    instances = []

    class FakeBrowserAuth:
        def __init__(self, *args, **kwargs):
            self.session = None
            self.disconnect_calls = 0
            instances.append(self)

        async def open_browser(self):
            self.session = session
            return worker.AuthStatus(state="connected", message="connected")

        async def disconnect(self):
            self.disconnect_calls += 1
            self.session = None
            return worker.AuthStatus(state="disconnected", message="disconnected")

        async def open_disconnect_browser(self):
            return await self.disconnect()

        def status(self):
            state = "connected" if self.session is not None else "disconnected"
            return worker.AuthStatus(state=state, message=state)

        async def aclose(self):
            return None

    async def order_depth(auth, command):
        assert auth.session is session
        clock["now"] = worker._MEMORY_ONLY_IDLE_SECONDS + 1
        return {"ok": True, "result": {"bids": [], "asks": []}}

    def feed(_loop, queue):
        queue.put_nowait(json.dumps({"action": "connect"}))
        queue.put_nowait(json.dumps({"action": "order_depth", "order_book_id": "123"}))
        queue.put_nowait(json.dumps({"action": "shutdown"}))
        queue.put_nowait(None)

    monkeypatch.setattr(worker, "BrowserAuth", FakeBrowserAuth)
    monkeypatch.setattr(worker, "_order_depth_operation", order_depth)
    monkeypatch.setattr(worker, "_start_stdin_reader", feed)
    monkeypatch.setattr(worker, "_parent_alive", lambda _pid: True)
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock["now"])

    await worker._run_daemon("memory_only", 123)

    payloads = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert [payload["status"]["state"] if "status" in payload else "result" for payload in payloads] == [
        "connected",
        "result",
    ]
    assert len(instances) == 1
    assert instances[0].disconnect_calls == 1


def test_authenticated_market_batch_pacing_is_50ms():
    assert worker._MARKET_BATCH_MIN_REQUEST_INTERVAL == 0.05



def test_authenticated_market_batch_concurrency_is_16():
    assert worker._MARKET_BATCH_CONCURRENCY == 16


async def test_market_batch_fetches_with_bounded_concurrency_and_preserves_order(monkeypatch):
    seen_paths = []
    seen_concurrency = []

    class FakeResponse:
        def __init__(self, value):
            self.status_code = 200
            self._value = value

        def json(self):
            return {"quote": {"buy": self._value, "sell": self._value + 0.1}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            assert kwargs["max_connections"] == worker._MARKET_BATCH_CONCURRENCY
            assert kwargs["max_in_flight_requests"] == worker._MARKET_BATCH_CONCURRENCY
            assert kwargs["min_request_interval"] == worker._MARKET_BATCH_MIN_REQUEST_INTERVAL

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request_authenticated_batch(self, paths, *, max_concurrency):
            seen_paths.append(list(paths))
            seen_concurrency.append(max_concurrency)
            return [
                FakeResponse(int(path.rsplit("/", 1)[-1]))
                for path in paths
            ]

    monkeypatch.setattr(worker, "AvanzaClient", FakeClient)
    auth = worker._RequestAuth(SessionMaterial((), "token"))
    paths = [
        f"/_api/trading-critical/rest/marketdata/{index}" for index in range(1, 25)
    ]
    result = await worker._market_batch_operation(auth, {"paths": paths})

    assert result["ok"] is True
    assert [item["quote"]["buy"] for item in result["result"]] == list(range(1, 25))
    assert seen_paths == [paths]
    assert seen_concurrency == [worker._MARKET_BATCH_CONCURRENCY]


async def test_instrument_news_batch_preserves_order_and_explicit_failures(monkeypatch):
    from avanza_mcp.client.accounts import AccountReadError
    from avanza_mcp.models.account import InstrumentNews, NewsArticle

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeAccountClient:
        def __init__(self, client):
            assert isinstance(client, FakeClient)

        async def news(self, order_book_id, limit):
            assert limit == 3
            if order_book_id == "2":
                raise AccountReadError("http_500")
            await asyncio.sleep(0)
            return InstrumentNews(
                articles=[
                    NewsArticle(
                        published_at="2026-09-30",
                        headline=f"news-{order_book_id}",
                    )
                ],
                truncated=False,
            )

    monkeypatch.setattr(worker, "AvanzaClient", FakeClient)
    monkeypatch.setattr(worker, "AccountClient", FakeAccountClient)
    auth = worker._RequestAuth(SessionMaterial((), "token"))

    result = await worker._account_operation(
        auth,
        "instrument_news_batch",
        {"order_book_ids": ["1", "2", "3"], "limit_per_instrument": 3},
    )

    assert result["ok"] is True
    assert [item["order_book_id"] for item in result["result"]["items"]] == ["1", "3"]
    assert [
        item["articles"][0]["headline"] for item in result["result"]["items"]
    ] == ["news-1", "news-3"]
    assert result["result"]["failed_order_book_ids"] == ["2"]


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


async def test_memory_daemon_fail_safe_disconnects_on_unexpected_error(monkeypatch):
    session = SessionMaterial((), "token")
    instances = []

    class FakeBrowserAuth:
        def __init__(self, *args, **kwargs):
            self.session = None
            self.disconnect_calls = 0
            self.close_calls = 0
            instances.append(self)

        async def open_browser(self):
            self.session = session
            raise RuntimeError("synthetic failure")

        async def disconnect(self):
            self.disconnect_calls += 1
            self.session = None
            return None

        async def aclose(self):
            self.close_calls += 1

    def feed(loop, queue):
        loop.call_soon(queue.put_nowait, json.dumps({"action": "connect"}))

    monkeypatch.setattr(worker, "BrowserAuth", FakeBrowserAuth)
    monkeypatch.setattr(worker, "_start_stdin_reader", feed)
    monkeypatch.setattr(worker, "_parent_alive", lambda _pid: True)

    try:
        await worker._run_daemon("memory_only", 123)
    except RuntimeError as exc:
        assert str(exc) == "synthetic failure"
    else:
        raise AssertionError("expected synthetic failure")

    assert len(instances) == 1
    assert instances[0].disconnect_calls == 1
    assert instances[0].close_calls == 1


async def test_transactions_account_operation_validates_filters_and_delegates(monkeypatch):
    from avanza_mcp.models.account import Transactions

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    seen = {}

    class FakeAccountClient:
        def __init__(self, client):
            assert isinstance(client, FakeClient)

        async def transactions(
            self, *, from_date, to_date, limit, isin=None, transaction_types=None
        ):
            seen["args"] = {
                "from_date": from_date,
                "to_date": to_date,
                "limit": limit,
                "isin": isin,
                "transaction_types": transaction_types,
            }
            return Transactions(
                transactions=[],
                returned=0,
                total_reported=0,
                truncated=False,
            )

    monkeypatch.setattr(worker, "AvanzaClient", FakeClient)
    monkeypatch.setattr(worker, "AccountClient", FakeAccountClient)
    auth = worker._RequestAuth(SessionMaterial((), "token"))

    result = await worker._account_operation(
        auth,
        "transactions",
        {
            "from_date": "2026-01-01",
            "to_date": "2026-01-31",
            "limit": 25,
            "isin": "SE0000115446",
            "transaction_types": ["BUY", "SELL"],
        },
    )

    assert result["ok"] is True
    assert seen["args"]["limit"] == 25
    assert seen["args"]["isin"] == "SE0000115446"
    assert seen["args"]["transaction_types"] == ["BUY", "SELL"]


async def test_transactions_account_operation_rejects_unknown_type(monkeypatch):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(worker, "AvanzaClient", FakeClient)
    auth = worker._RequestAuth(SessionMaterial((), "token"))

    result = await worker._account_operation(
        auth,
        "transactions",
        {"transaction_types": ["BUY", "NOT_A_TYPE"]},
    )

    assert result == {"ok": False, "code": "read_error_response_shape"}


async def test_forum_posts_account_operation_is_bounded_and_delegates(monkeypatch):
    from avanza_mcp.models.account import ForumPost, ForumPosts

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    seen = {}

    class FakeAccountClient:
        def __init__(self, client):
            assert isinstance(client, FakeClient)

        async def forum_posts(self, order_book_id, limit):
            seen["args"] = (order_book_id, limit)
            return ForumPosts(
                posts=[
                    ForumPost(
                        author="Synthetic user",
                        title="Synthetic title",
                        content="Synthetic discussion",
                    )
                ],
                truncated=False,
            )

    monkeypatch.setattr(worker, "AvanzaClient", FakeClient)
    monkeypatch.setattr(worker, "AccountClient", FakeAccountClient)
    auth = worker._RequestAuth(SessionMaterial((), "token"))

    result = await worker._account_operation(
        auth,
        "forum_posts",
        {"order_book_id": "123", "limit": 7},
    )

    assert result["ok"] is True
    assert result["result"]["posts"][0]["title"] == "Synthetic title"
    assert seen["args"] == ("123", 7)


async def test_forum_post_operation_requires_explicit_confirmation():
    class FakeAuth:
        session = SessionMaterial((), "forum-token")

    result = await worker._forum_post_operation(
        FakeAuth(),
        {
            "isin": "SE0000115446",
            "title": "Title",
            "content": "Exact body",
            "confirm": False,
        },
    )

    assert result == {"ok": False, "code": "confirmation_required"}


async def test_forum_post_operation_keeps_token_inside_worker(monkeypatch):
    seen = {}

    class FakeAuth:
        session = SessionMaterial((), "forum-token")

        async def disconnect(self):
            self.session = None

    class FakeReceipt:
        post_id = "post-1"
        instrument_name = "Volvo B"
        instrument_slug = "volvo-b"
        company_name = "Volvo"
        company_slug = "volvo"

    class FakeForumClient:
        def __init__(self, token):
            seen["token"] = token

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def create_post(self, *, isin, title, content):
            seen["request"] = {
                "isin": isin,
                "title": title,
                "content": content,
            }
            return FakeReceipt()

    monkeypatch.setattr(worker, "ForumAPIClient", FakeForumClient)

    result = await worker._forum_post_operation(
        FakeAuth(),
        {
            "isin": "SE0000115446",
            "title": "Title",
            "content": "Exact body",
            "confirm": True,
        },
    )

    assert seen == {
        "token": "forum-token",
        "request": {
            "isin": "SE0000115446",
            "title": "Title",
            "content": "Exact body",
        },
    }
    assert result == {
        "ok": True,
        "result": {
            "post_id": "post-1",
            "instrument_name": "Volvo B",
            "instrument_slug": "volvo-b",
            "company_name": "Volvo",
            "company_slug": "volvo",
        },
    }
    assert "forum-token" not in str(result)
