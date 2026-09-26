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
from typing import Any

from ..client.accounts import AccountAuthExpired, AccountClient, AccountReadError
from ..client.base import AvanzaClient, _authenticated_market_kind
from ..client.exceptions import AvanzaAuthError
from .browser import AuthStatus, BrowserAuth
from .store import AuthStoreError, create_session_store

_INTERNAL_BROWSER_IDLE_SECONDS = 365 * 24 * 60 * 60
_MEMORY_ONLY_IDLE_SECONDS = 15 * 60
_ONE_SHOT_IDLE_SECONDS = 5 * 60
_TERMINAL_STATES = frozenset({"connected", "disconnected", "denied", "timed_out", "error"})


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


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


async def _account_operation(
    auth: BrowserAuth, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    if auth.session is None:
        return {"ok": False, "code": "auth_required"}

    try:
        async with AvanzaClient(
            session_provider=lambda: auth.session,
            session_invalidated=auth.invalidate_session,
        ) as client:
            account = AccountClient(client)
            if operation == "accounts":
                result = await account.accounts()
            elif operation == "holdings":
                result = await account.holdings()
            elif operation == "transactions":
                raw_from = arguments.get("from_date")
                raw_to = arguments.get("to_date")
                result = await account.transactions(
                    from_date=date.fromisoformat(raw_from) if raw_from else None,
                    to_date=date.fromisoformat(raw_to) if raw_to else None,
                    limit=int(arguments.get("limit", 100)),
                )
            elif operation == "credit_info":
                result = await account.credit_info(str(arguments.get("credit_type", "credited")))
            elif operation == "watchlists":
                result = await account.watchlists()
            elif operation == "price_alerts":
                result = await account.price_alerts(str(arguments["order_book_id"]))
            elif operation == "current_offers":
                result = await account.offers()
            elif operation == "portfolio_insights":
                accounts = await account.accounts()
                result = await account.insights(
                    [item.account_id for item in accounts.accounts],
                    str(arguments.get("time_period", "THIS_YEAR")),
                )
            elif operation == "instrument_news":
                result = await account.news(
                    str(arguments["order_book_id"]),
                    int(arguments.get("limit", 20)),
                )
            elif operation == "forum_posts":
                result = await account.forum_posts(
                    str(arguments["order_book_id"]),
                    int(arguments.get("limit", 20)),
                )
            elif operation == "insider_transactions":
                result = await account.insider_transactions(
                    str(arguments["order_book_id"]),
                    int(arguments.get("limit", 20)),
                )
            elif operation == "active_orders":
                result = await account.active_orders(int(arguments.get("limit", 100)))
            elif operation == "deals":
                result = await account.deals(int(arguments.get("limit", 100)))
            elif operation == "stop_loss_orders":
                result = await account.stop_losses(int(arguments.get("limit", 100)))
            else:
                return {"ok": False, "code": "operation_not_allowed"}
    except AccountAuthExpired:
        await auth.invalidate_session()
        return {"ok": False, "code": "auth_expired"}
    except (AccountReadError, ValueError, KeyError, TypeError):
        return {"ok": False, "code": "read_error"}
    except Exception:
        return {"ok": False, "code": "worker_error"}

    return {"ok": True, "result": result.model_dump(mode="json")}


async def _market_operation(
    auth: BrowserAuth, command: dict[str, Any]
) -> dict[str, Any]:
    method = str(command.get("method", ""))
    path = str(command.get("path", ""))
    if _authenticated_market_kind(method, path) is None:
        return {"ok": False, "code": "operation_not_allowed"}
    if auth.session is None:
        return {"ok": False, "code": "no_session"}

    params = command.get("params")
    if params is not None and not isinstance(params, dict):
        return {"ok": False, "code": "protocol_error"}

    try:
        async with AvanzaClient(
            session_provider=lambda: auth.session,
            session_invalidated=auth.invalidate_session,
        ) as client:
            result = await client.get(path, params=params)
    except AvanzaAuthError:
        await auth.invalidate_session()
        return {"ok": False, "code": "auth_expired"}
    except Exception:
        return {"ok": False, "code": "read_error"}
    return {"ok": True, "result": result}


async def _run_once(command: dict[str, Any]) -> None:
    store = create_session_store()
    auth = BrowserAuth(
        store=store,
        session_idle_seconds=_INTERNAL_BROWSER_IDLE_SECONDS,
    )
    try:
        state, status = await _restore_persistent(auth, store)
        action = command.get("action")
        if action == "status":
            _emit(_safe_status(status))
            return
        if state == "none":
            _emit({"ok": False, "code": "no_session"})
            return
        if state == "expired":
            _emit({"ok": False, "code": "auth_expired"})
            return
        if state != "connected":
            _emit({"ok": False, "code": status.error_code or "worker_error"})
            return

        if action == "account":
            operation = command.get("operation")
            arguments = command.get("arguments", {})
            if not isinstance(operation, str) or not isinstance(arguments, dict):
                _emit({"ok": False, "code": "protocol_error"})
                return
            _emit(await _account_operation(auth, operation, arguments))
            return

        if action == "market":
            _emit(await _market_operation(auth, command))
            return

        _emit({"ok": False, "code": "operation_not_allowed"})
    finally:
        await auth.aclose()


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
                _emit(await _market_operation(auth, command))
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
