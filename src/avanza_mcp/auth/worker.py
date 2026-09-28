"""Isolated process that is the only runtime allowed to hold Avanza session material."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from datetime import date
from typing import Any, TextIO

from ..client.accounts import AccountAuthExpired, AccountClient, AccountReadError
from ..client.bankid import BankIDClient, BankIDError, SessionMaterial
from ..client.base import (
    AvanzaClient,
    _authenticated_market_kind,
    _project_authenticated_market_payload,
)
from ..client.endpoints import authenticated_public_request_allowed
from ..client.exceptions import AvanzaAuthError
from .browser import AuthStatus, BrowserAuth
from .store import AuthStoreError, create_session_store

_INTERNAL_BROWSER_IDLE_SECONDS = 365 * 24 * 60 * 60
_MARKET_BATCH_CONCURRENCY = 8
_MEMORY_ONLY_IDLE_SECONDS = 15 * 60
_ONE_SHOT_IDLE_SECONDS = 5 * 60
_TERMINAL_STATES = frozenset({"connected", "disconnected", "denied", "timed_out", "error"})
_ALLOWED_ACCOUNT_OPERATIONS = frozenset(
    {
        "accounts",
        "holdings",
        "transactions",
        "watchlists",
        "price_alerts",
        "portfolio_insights",
        "portfolio_snapshot",
        "instrument_news",
        "insider_transactions",
        "active_orders",
        "deals",
        "stop_loss_orders",
    }
)
_FORBIDDEN_MARKET_RESULT_KEYS = frozenset(
    {
        "cookies",
        "securitytoken",
        "security_token",
        "authenticationsession",
        "authentication_session",
        "sessionid",
        "session_id",
        "authorization",
        "x_securitytoken",
        "x_security_token",
        "set_cookie",
    }
)


class _RequestAuth:
    """One operation-local session holder; persistence is owned by the worker."""

    def __init__(self, session: SessionMaterial) -> None:
        self.session: SessionMaterial | None = session

    async def invalidate_session(self) -> None:
        self.session = None


def _cookie_state(cookie: Any) -> tuple[Any, ...]:
    return (
        cookie.version,
        cookie.name,
        cookie.value,
        cookie.port,
        cookie.port_specified,
        cookie.domain,
        cookie.domain_specified,
        cookie.domain_initial_dot,
        cookie.path,
        cookie.path_specified,
        cookie.secure,
        cookie.expires,
        cookie.discard,
        cookie.comment,
        cookie.comment_url,
        tuple(sorted(cookie._rest.items())),
        cookie.rfc2109,
    )


def _same_session_material(left: SessionMaterial, right: SessionMaterial) -> bool:
    return (
        left._security_token == right._security_token
        and tuple(_cookie_state(item) for item in left._cookies)
        == tuple(_cookie_state(item) for item in right._cookies)
    )


async def _validate_saved_session(
    session: SessionMaterial,
) -> tuple[SessionMaterial | None, str | None]:
    attempt = BankIDClient()
    try:
        return await attempt.validate_session(session), None
    except BankIDError as error:
        code = (
            f"{error.code.value}_http_{error.upstream_status}"
            if error.upstream_status is not None
            else error.code.value
        )
        return None, code
    finally:
        await attempt.aclose()


def _contains_forbidden_market_result(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in _FORBIDDEN_MARKET_RESULT_KEYS:
                return True
            if _contains_forbidden_market_result(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_market_result(item) for item in value)
    return False


_PORTFOLIO_PERIODS = frozenset(
    {"TODAY", "ONE_WEEK", "THIS_YEAR", "THREE_YEARS_ROLLING"}
)


_PROTOCOL_STDOUT: TextIO | None = None


def _isolate_protocol_stdout() -> None:
    """Reserve the original stdout pipe for worker protocol frames only.

    Third-party code may write diagnostics to stdout. Duplicate the broker pipe,
    then redirect ordinary fd 1 to stderr so such output cannot corrupt IPC.
    """
    global _PROTOCOL_STDOUT
    if _PROTOCOL_STDOUT is not None:
        return
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    _PROTOCOL_STDOUT = os.fdopen(
        protocol_fd,
        "w",
        encoding="utf-8",
        buffering=1,
        closefd=True,
    )
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())


def _emit(value: dict[str, Any]) -> None:
    stream = _PROTOCOL_STDOUT or sys.stdout
    stream.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
    stream.flush()


def _safe_status(status: AuthStatus) -> dict[str, Any]:
    return {"ok": True, "status": status.model_dump(mode="json")}


def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, 0, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Access denied still proves that a process with this PID exists.
        return ctypes.get_last_error() == 5

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _restore_persistent(
    auth: BrowserAuth, store: Any
) -> tuple[str, AuthStatus]:
    try:
        saved = await store.load()
    except AuthStoreError:
        return (
            "store_error",
            AuthStatus(
                state="error",
                message="The OS credential store is unavailable.",
                error_code="credential_store",
            ),
        )
    if saved is None:
        return "none", AuthStatus(state="disconnected", message="Avanza is not connected.")

    status = await auth.restore()
    if status.state == "connected" and auth.session is not None:
        return "connected", status
    if status.state == "disconnected":
        return "expired", status
    return "error", status


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool):
        raise ValueError
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError
    return parsed


def _numeric_order_book_id(value: Any) -> str:
    parsed = str(value)
    if not parsed or not parsed.isascii() or not parsed.isdecimal():
        raise ValueError
    return parsed


def _only_arguments(arguments: dict[str, Any], allowed: set[str] | frozenset[str]) -> None:
    if set(arguments) - set(allowed):
        raise ValueError


def _safe_account_read_failure_code(error: AccountReadError) -> str:
    """Reduce account read failures to a small credential-free diagnostic vocabulary."""
    raw = str(error)
    if raw in {"network", "invalid_json", "response_too_large", "request_not_allowed"}:
        return f"read_error_{raw}"
    if raw.startswith("http_") and len(raw) == 8 and raw[5:].isdigit():
        return f"read_error_{raw}"
    return "read_error_response_shape"


async def _account_operation(
    auth: BrowserAuth | _RequestAuth, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    if operation not in _ALLOWED_ACCOUNT_OPERATIONS:
        return {"ok": False, "code": "operation_not_allowed"}
    if auth.session is None:
        return {"ok": False, "code": "auth_required"}

    try:
        async with AvanzaClient(
            session_provider=lambda: auth.session,
            session_invalidated=auth.invalidate_session,
        ) as client:
            account = AccountClient(client)

            if operation == "accounts":
                _only_arguments(arguments, frozenset())
                result = await account.accounts()
            elif operation == "holdings":
                _only_arguments(arguments, frozenset())
                result = await account.holdings()
            elif operation == "transactions":
                _only_arguments(arguments, {"from_date", "to_date", "limit"})
                raw_from = arguments.get("from_date")
                raw_to = arguments.get("to_date")
                from_date = date.fromisoformat(raw_from) if raw_from else None
                to_date = date.fromisoformat(raw_to) if raw_to else None
                if from_date is not None and to_date is not None and from_date > to_date:
                    raise ValueError
                result = await account.transactions(
                    from_date=from_date,
                    to_date=to_date,
                    limit=_bounded_int(
                        arguments.get("limit"), default=100, minimum=1, maximum=1000
                    ),
                )
            elif operation == "watchlists":
                _only_arguments(arguments, frozenset())
                result = await account.watchlists()
            elif operation == "price_alerts":
                _only_arguments(arguments, {"order_book_id"})
                result = await account.price_alerts(
                    _numeric_order_book_id(arguments["order_book_id"])
                )
            elif operation == "portfolio_insights":
                _only_arguments(arguments, {"time_period"})
                time_period = str(arguments.get("time_period", "THIS_YEAR"))
                if time_period not in _PORTFOLIO_PERIODS:
                    raise ValueError
                accounts = await account.accounts()
                result = await account.insights(
                    [item.account_id for item in accounts.accounts],
                    time_period,
                )
            elif operation == "portfolio_snapshot":
                _only_arguments(arguments, {"limit"})
                result = await account.portfolio_snapshot(
                    _bounded_int(
                        arguments.get("limit"), default=100, minimum=1, maximum=100
                    )
                )
            elif operation == "instrument_news":
                _only_arguments(arguments, {"order_book_id", "limit"})
                result = await account.news(
                    _numeric_order_book_id(arguments["order_book_id"]),
                    _bounded_int(
                        arguments.get("limit"), default=20, minimum=1, maximum=100
                    ),
                )
            elif operation == "insider_transactions":
                _only_arguments(arguments, {"order_book_id", "limit"})
                result = await account.insider_transactions(
                    _numeric_order_book_id(arguments["order_book_id"]),
                    _bounded_int(
                        arguments.get("limit"), default=20, minimum=1, maximum=100
                    ),
                )
            elif operation == "active_orders":
                _only_arguments(arguments, {"limit"})
                result = await account.active_orders(
                    _bounded_int(
                        arguments.get("limit"), default=100, minimum=1, maximum=100
                    )
                )
            elif operation == "deals":
                _only_arguments(arguments, {"limit"})
                result = await account.deals(
                    _bounded_int(
                        arguments.get("limit"), default=100, minimum=1, maximum=100
                    )
                )
            else:
                _only_arguments(arguments, {"limit"})
                result = await account.stop_losses(
                    _bounded_int(
                        arguments.get("limit"), default=100, minimum=1, maximum=100
                    )
                )
    except AccountAuthExpired:
        await auth.invalidate_session()
        return {"ok": False, "code": "auth_expired"}
    except AccountReadError as error:
        return {"ok": False, "code": _safe_account_read_failure_code(error)}
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "code": "read_error_response_shape"}
    except Exception as error:
        name = type(error).__name__
        if name.isidentifier() and len(name) <= 64:
            return {"ok": False, "code": f"worker_error_{name}"}
        return {"ok": False, "code": "worker_error"}

    return {"ok": True, "result": result.model_dump(mode="json")}

async def _market_operation(
    auth: BrowserAuth | _RequestAuth, command: dict[str, Any]
) -> dict[str, Any]:
    method = str(command.get("method", ""))
    path = str(command.get("path", ""))
    if not authenticated_public_request_allowed(method, path):
        return {"ok": False, "code": "operation_not_allowed"}
    if auth.session is None:
        return {"ok": False, "code": "no_session"}

    params = command.get("params")
    json_body = command.get("json")
    if params is not None and not isinstance(params, dict):
        return {"ok": False, "code": "protocol_error"}
    if json_body is not None and not isinstance(json_body, dict):
        return {"ok": False, "code": "protocol_error"}

    try:
        async with AvanzaClient(
            session_provider=lambda: auth.session,
            session_invalidated=auth.invalidate_session,
        ) as client:
            response = await client.request_authenticated(
                method,
                path,
                params=params,
                json=json_body,
            )
            if response.status_code == 401:
                await auth.invalidate_session()
                return {"ok": False, "code": "auth_expired"}
            if response.status_code != 200:
                return {"ok": False, "code": "read_error"}
            result = response.json()
            if not isinstance(result, (dict, list)):
                return {"ok": False, "code": "read_error"}
            kind = _authenticated_market_kind(method, path)
            if kind is not None:
                result = _project_authenticated_market_payload(kind, result)
            elif _contains_forbidden_market_result(result):
                return {"ok": False, "code": "unsafe_upstream_payload"}
    except AvanzaAuthError:
        await auth.invalidate_session()
        return {"ok": False, "code": "auth_expired"}
    except Exception:
        return {"ok": False, "code": "read_error"}
    return {"ok": True, "result": result}


async def _market_batch_operation(
    auth: BrowserAuth | _RequestAuth, command: dict[str, Any]
) -> dict[str, Any]:
    """Fetch trading-critical quotes concurrently inside one validated auth worker."""
    paths = command.get("paths")
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        return {"ok": False, "code": "protocol_error"}
    if any(
        authenticated_public_request_allowed("GET", path) is False
        or _authenticated_market_kind("GET", path) != "marketdata"
        for path in paths
    ):
        return {"ok": False, "code": "operation_not_allowed"}
    if auth.session is None:
        return {"ok": False, "code": "no_session"}

    try:
        async with AvanzaClient(
            session_provider=lambda: auth.session,
            session_invalidated=auth.invalidate_session,
            max_connections=_MARKET_BATCH_CONCURRENCY,
            max_keepalive_connections=_MARKET_BATCH_CONCURRENCY,
            max_in_flight_requests=_MARKET_BATCH_CONCURRENCY,
        ) as client:
            responses = await client.request_authenticated_batch(
                paths,
                max_concurrency=_MARKET_BATCH_CONCURRENCY,
            )
    except AvanzaAuthError:
        await auth.invalidate_session()
        return {"ok": False, "code": "auth_expired"}
    except Exception:
        return {"ok": False, "code": "read_error"}

    results: list[dict[str, Any] | None] = []
    for response in responses:
        if response is None:
            results.append(None)
            continue
        if response.status_code == 401:
            await auth.invalidate_session()
            return {"ok": False, "code": "auth_expired"}
        if response.status_code != 200:
            results.append(None)
            continue
        try:
            payload = response.json()
            projected = _project_authenticated_market_payload("marketdata", payload)
        except (ValueError, TypeError):
            results.append(None)
            continue
        if _contains_forbidden_market_result(projected):
            return {"ok": False, "code": "unsafe_upstream_payload"}
        if not isinstance(projected, dict):
            results.append(None)
            continue
        results.append(projected)

    return {"ok": True, "result": results}


async def _run_once(command: dict[str, Any]) -> None:
    store = create_session_store()
    action = command.get("action")

    if action == "status":
        auth = BrowserAuth(
            store=store,
            session_idle_seconds=_INTERNAL_BROWSER_IDLE_SECONDS,
        )
        try:
            _, status = await _restore_persistent(auth, store)
            _emit(_safe_status(status))
            return
        finally:
            await auth.aclose()

    if action == "account":
        operation = command.get("operation")
        arguments = command.get("arguments", {})
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            _emit({"ok": False, "code": "protocol_error"})
            return
    elif action not in {"market", "market_batch"}:
        _emit({"ok": False, "code": "operation_not_allowed"})
        return

    try:
        saved = await store.load()
    except AuthStoreError:
        _emit({"ok": False, "code": "credential_store"})
        return
    if saved is None:
        _emit({"ok": False, "code": "no_session"})
        return

    request_auth = _RequestAuth(saved)
    validation_task = asyncio.create_task(_validate_saved_session(saved))
    if action == "account":
        operation_task = asyncio.create_task(
            _account_operation(request_auth, operation, arguments)
        )
    elif action == "market_batch":
        operation_task = asyncio.create_task(
            _market_batch_operation(request_auth, command)
        )
    else:
        operation_task = asyncio.create_task(_market_operation(request_auth, command))

    validated, result = await asyncio.gather(validation_task, operation_task)
    refreshed, validation_error = validated

    if validation_error is not None:
        # Preserve the existing fail-closed behavior when session validation
        # itself cannot be completed. Never return an operation result whose
        # concurrent validation was inconclusive.
        _emit({"ok": False, "code": validation_error})
        return

    if refreshed is None:
        try:
            await store.delete()
        except AuthStoreError:
            _emit({"ok": False, "code": "credential_store"})
            return
        _emit({"ok": False, "code": "auth_expired"})
        return

    if not _same_session_material(saved, refreshed):
        try:
            await store.save(refreshed)
        except AuthStoreError:
            _emit({"ok": False, "code": "credential_store"})
            return

    if result.get("code") == "auth_expired":
        # The session-info request may have refreshed cookies/token that the
        # concurrently started operation did not yet have. Retry once with the
        # verified material before declaring the saved session expired.
        retry_auth = _RequestAuth(refreshed)
        if action == "account":
            result = await _account_operation(retry_auth, operation, arguments)
        elif action == "market_batch":
            result = await _market_batch_operation(retry_auth, command)
        else:
            result = await _market_operation(retry_auth, command)

        if result.get("code") == "auth_expired":
            try:
                await store.delete()
            except AuthStoreError:
                _emit({"ok": False, "code": "credential_store"})
                return

    _emit(result)

async def _run_ui(command: dict[str, Any], parent_pid: int) -> None:
    store = create_session_store()
    auth = BrowserAuth(
        store=store,
        session_idle_seconds=_INTERNAL_BROWSER_IDLE_SECONDS,
    )
    try:
        state, restored = await _restore_persistent(auth, store)
        action = command.get("action")

        if action == "connect":
            if state in {"store_error", "error"}:
                _emit(_safe_status(restored))
                return
            status = await auth.open_browser()
        elif action == "disconnect":
            if state in {"store_error", "error"}:
                _emit(_safe_status(restored))
                return
            status = await auth.open_disconnect_browser()
        else:
            _emit({"ok": False, "code": "operation_not_allowed"})
            return

        _emit(_safe_status(status))
        if status.state in _TERMINAL_STATES:
            return

        while _parent_alive(parent_pid):
            current = auth.status()
            if current.state in _TERMINAL_STATES:
                _emit(_safe_status(current))
                # Keep the success/failure page reachable long enough to render.
                await asyncio.sleep(2.2)
                return
            await asyncio.sleep(0.25)
    finally:
        await auth.aclose()


def _start_stdin_reader(
    loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str | None]
) -> None:
    def reader() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, name="avanza-auth-stdin", daemon=True).start()


async def _run_daemon(mode: str, parent_pid: int) -> None:
    if mode not in {"memory_only", "one_shot"}:
        _emit({"ok": False, "code": "invalid_mode"})
        return

    idle_seconds = (
        _MEMORY_ONLY_IDLE_SECONDS if mode == "memory_only" else _ONE_SHOT_IDLE_SECONDS
    )
    auth = BrowserAuth(
        store=None,
        session_idle_seconds=_INTERNAL_BROWSER_IDLE_SECONDS,
    )
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    _start_stdin_reader(loop, queue)
    last_activity: float | None = None

    try:
        while True:
            if not _parent_alive(parent_pid):
                if auth.session is not None:
                    await auth.disconnect()
                return

            if auth.session is not None and last_activity is None:
                last_activity = time.monotonic()
            if auth.session is None:
                last_activity = None
            if (
                auth.session is not None
                and last_activity is not None
                and time.monotonic() - last_activity >= idle_seconds
            ):
                await auth.disconnect()
                return

            try:
                line = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            if line is None:
                if auth.session is not None:
                    await auth.disconnect()
                return

            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                _emit({"ok": False, "code": "protocol_error"})
                continue
            if not isinstance(command, dict):
                _emit({"ok": False, "code": "protocol_error"})
                continue

            action = command.get("action")
            if action == "connect":
                _emit(_safe_status(await auth.open_browser()))
                continue
            if action == "disconnect":
                _emit(_safe_status(await auth.open_disconnect_browser()))
                continue
            if action == "status":
                _emit(_safe_status(auth.status()))
                continue
            if action == "shutdown":
                if auth.session is not None:
                    status = await auth.disconnect()
                else:
                    status = AuthStatus(
                        state="disconnected", message="Avanza is not connected."
                    )
                _emit(_safe_status(status))
                return
            if action == "account":
                operation = command.get("operation")
                arguments = command.get("arguments", {})
                if not isinstance(operation, str) or not isinstance(arguments, dict):
                    _emit({"ok": False, "code": "protocol_error"})
                    continue
                response = await _account_operation(auth, operation, arguments)
                if response.get("ok") is True:
                    last_activity = time.monotonic()
                    if mode == "one_shot":
                        await auth.disconnect()
                        _emit(response)
                        return
                _emit(response)
                continue
            if action == "market":
                response = await _market_operation(auth, command)
                if response.get("ok") is True and mode == "memory_only":
                    last_activity = time.monotonic()
                _emit(response)
                continue
            if action == "market_batch":
                response = await _market_batch_operation(auth, command)
                if response.get("ok") is True and mode == "memory_only":
                    last_activity = time.monotonic()
                _emit(response)
                continue

            _emit({"ok": False, "code": "operation_not_allowed"})
    finally:
        await auth.aclose()


async def _read_first_command() -> dict[str, Any] | None:
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


async def _async_main(args: argparse.Namespace) -> int:
    if args.kind == "daemon":
        await _run_daemon(args.mode, args.parent_pid)
        return 0

    command = await _read_first_command()
    if command is None:
        _emit({"ok": False, "code": "protocol_error"})
        return 2

    if args.kind == "once":
        if args.mode != "persistent":
            _emit({"ok": False, "code": "invalid_mode"})
            return 2
        await _run_once(command)
        return 0

    if args.kind == "ui":
        if args.mode != "persistent":
            _emit({"ok": False, "code": "invalid_mode"})
            return 2
        await _run_ui(command, args.parent_pid)
        return 0

    _emit({"ok": False, "code": "invalid_kind"})
    return 2


def main() -> int:
    _isolate_protocol_stdout()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--kind", choices=("once", "ui", "daemon"), required=True)
    parser.add_argument(
        "--mode",
        choices=("persistent", "memory_only", "one_shot"),
        required=True,
    )
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
