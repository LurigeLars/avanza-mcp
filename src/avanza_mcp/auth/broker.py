"""Credential-isolating subprocess broker for authenticated Avanza operations."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from time import monotonic
from typing import Any, Literal

import httpx

from ..client.endpoints import authenticated_public_request_family
from .browser import AuthStatus

SessionMode = Literal["persistent", "memory_only", "one_shot"]
SESSION_MODES = frozenset({"persistent", "memory_only", "one_shot"})

_PIPE_LIMIT = 4 * 1024 * 1024
_COMMAND_TIMEOUT = 90.0
_UI_RESPONSE_TIMEOUT = 10.0
_MARKET_AUTH_FAILURE_BACKOFF_SECONDS = 60.0
_FORBIDDEN_RESULT_KEYS = frozenset(
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


class AuthWorkerError(RuntimeError):
    """Base class for sanitized worker failures."""


class AuthWorkerRequired(AuthWorkerError):
    """No reusable authenticated Avanza session exists."""


class AuthWorkerExpired(AuthWorkerError):
    """A previously available authenticated session is no longer valid."""


class AuthWorkerOperationError(AuthWorkerError):
    """The isolated worker could not complete an approved operation."""


def session_mode_from_environment() -> SessionMode:
    value = os.environ.get("AVANZA_SESSION_MODE", "memory_only").strip().lower()
    if value not in SESSION_MODES:
        allowed = ", ".join(sorted(SESSION_MODES))
        raise RuntimeError(f"AVANZA_SESSION_MODE must be one of: {allowed}")
    return value  # type: ignore[return-value]


def _disconnected_status() -> AuthStatus:
    return AuthStatus(state="disconnected", message="Avanza is not connected.")


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in _FORBIDDEN_RESULT_KEYS:
                return True
            if _contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


class AuthProcessBroker:
    """Keep reusable Avanza credentials outside the long-lived MCP process."""

    def __init__(self, mode: SessionMode | None = None) -> None:
        self.mode: SessionMode = mode or session_mode_from_environment()
        self._daemon: asyncio.subprocess.Process | None = None
        self._daemon_lock = asyncio.Lock()
        self._market_daemon: asyncio.subprocess.Process | None = None
        self._market_daemon_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._ui_process: asyncio.subprocess.Process | None = None
        self._ui_status: AuthStatus | None = None
        self._ui_action: str | None = None
        self._ui_reaper: asyncio.Task[None] | None = None
        self._market_auth_backoff_until: dict[str, float] = {}
        self._closed = False

    async def connect(self) -> AuthStatus:
        self._ensure_open()
        if self.mode == "persistent":
            async with self._operation_lock:
                await self._stop_market_daemon()
                return await self._start_persistent_ui("connect")
        async with self._daemon_lock:
            process = await self._ensure_daemon_locked()
            response = await self._command(process, {"action": "connect"})
            return self._status_from_response(response)

    async def disconnect(self) -> AuthStatus:
        self._ensure_open()
        if self.mode == "persistent":
            # Wait for an already-started authenticated operation to finish before
            # opening the disconnect confirmation. New operations are blocked by
            # _ui_action until the UI worker reaches a terminal state.
            async with self._operation_lock:
                await self._stop_market_daemon()
                return await self._start_persistent_ui("disconnect")
        async with self._daemon_lock:
            process = self._live_daemon()
            if process is None:
                return _disconnected_status()
            response = await self._command(process, {"action": "disconnect"})
            return self._status_from_response(response)

    async def status(self) -> AuthStatus:
        self._ensure_open()
        if self.mode == "persistent":
            if self._ui_process is not None and self._ui_process.returncode is None:
                return self._ui_status or AuthStatus(
                    state="error",
                    message="Authentication flow is still running.",
                    error_code="auth_flow_busy",
                )
            response = await self._run_once({"action": "status"})
            return self._status_from_response(response)

        async with self._daemon_lock:
            process = self._live_daemon()
            if process is None:
                return _disconnected_status()
            response = await self._command(process, {"action": "status"})
            return self._status_from_response(response)

    async def account(self, operation: str, arguments: dict[str, Any]) -> Any:
        self._ensure_open()
        command = {
            "action": "account",
            "operation": operation,
            "arguments": arguments,
        }
        async with self._operation_lock:
            if self.mode == "persistent":
                if self._persistent_disconnect_active():
                    raise AuthWorkerOperationError("Disconnect confirmation is pending")
                await self._stop_market_daemon()
                response = await self._run_once(command)
            else:
                async with self._daemon_lock:
                    process = self._live_daemon()
                    if process is None:
                        raise AuthWorkerRequired
                    status_response = await self._command(process, {"action": "status"})
                    status = self._status_from_response(status_response)
                    if status.state == "awaiting_disconnect":
                        raise AuthWorkerOperationError("Disconnect confirmation is pending")
                    if status.state != "connected":
                        raise AuthWorkerRequired
                    response = await self._command(process, command)
            return self._result_from_response(response)

    async def warm_market_worker(self) -> None:
        """Prewarm the isolated persistent market worker without exposing session data."""
        self._ensure_open()
        if self.mode != "persistent":
            return
        async with self._operation_lock:
            if self._persistent_disconnect_active():
                return
            try:
                await self._persistent_market_command({"action": "warm"})
            except AuthWorkerOperationError:
                # Prewarming is best-effort; the normal market path will retry lazily.
                return

    async def market_request(
        self, method: str, path: str, kwargs: dict[str, Any]
    ) -> httpx.Response | None:
        """Use isolated auth for approved public read-only market-data requests."""
        family = authenticated_public_request_family(method, path)
        if family is None:
            return None

        retry_after = self._market_auth_backoff_until.get(family)
        now = monotonic()
        if retry_after is not None:
            if retry_after > now:
                return None
            self._market_auth_backoff_until.pop(family, None)

        command = {
            "action": "market",
            "method": method,
            "path": path,
            "params": kwargs.get("params"),
            "json": kwargs.get("json"),
        }
        try:
            async with self._operation_lock:
                if self.mode == "persistent":
                    if self._persistent_disconnect_active():
                        return None
                    response = await self._persistent_market_command(command)
                else:
                    async with self._daemon_lock:
                        process = self._live_daemon()
                        if process is None:
                            return None
                        status_response = await self._command(process, {"action": "status"})
                        status = self._status_from_response(status_response)
                        if status.state != "connected":
                            return None
                        response = await self._command(process, command)
        except AuthWorkerOperationError:
            # Avoid paying the same failed auth-worker cost on every page of a
            # batch. This cache contains only endpoint-family names, never
            # credentials or market payloads, and expires quickly so transient
            # failures are retried later.
            self._market_auth_backoff_until[family] = (
                monotonic() + _MARKET_AUTH_FAILURE_BACKOFF_SECONDS
            )
            return None

        if response.get("ok") is True:
            self._market_auth_backoff_until.pop(family, None)
            return httpx.Response(200, json=response.get("result"))
        code = response.get("code")
        if code == "no_session":
            return None
        if code in {"auth_required", "auth_expired"}:
            return httpx.Response(401, content=b"")
        # Endpoint-specific authenticated failures (for example unsupported
        # auth behavior or a credential-shaped upstream payload) must not break
        # the existing public read-only tool. Back off this endpoint family for
        # a short window so batch pagination can continue anonymously without
        # repeatedly spawning failed auth workers.
        self._market_auth_backoff_until[family] = (
            monotonic() + _MARKET_AUTH_FAILURE_BACKOFF_SECONDS
        )
        return None

    async def market_data_batch(
        self, paths: list[str]
    ) -> list[dict[str, Any] | None] | None:
        """Fetch reviewed trading-critical market data in one isolated worker."""
        self._ensure_open()
        if not paths:
            return []
        if any(
            authenticated_public_request_family("GET", path)
            != "trading_critical_market_data"
            for path in paths
        ):
            return None

        family = "trading_critical_market_data"
        retry_after = self._market_auth_backoff_until.get(family)
        now = monotonic()
        if retry_after is not None:
            if retry_after > now:
                return None
            self._market_auth_backoff_until.pop(family, None)

        command = {
            "action": "market_batch",
            "paths": list(paths),
        }
        try:
            async with self._operation_lock:
                if self.mode == "persistent":
                    if self._persistent_disconnect_active():
                        return None
                    response = await self._persistent_market_command(command)
                else:
                    async with self._daemon_lock:
                        process = self._live_daemon()
                        if process is None:
                            return None
                        status_response = await self._command(process, {"action": "status"})
                        status = self._status_from_response(status_response)
                        if status.state != "connected":
                            return None
                        response = await self._command(process, command)
        except AuthWorkerOperationError:
            self._market_auth_backoff_until[family] = (
                monotonic() + _MARKET_AUTH_FAILURE_BACKOFF_SECONDS
            )
            return None

        if response.get("ok") is True:
            raw = response.get("result")
            if (
                not isinstance(raw, list)
                or len(raw) != len(paths)
                or any(item is not None and not isinstance(item, dict) for item in raw)
            ):
                raise AuthWorkerOperationError("Invalid market batch result from worker")
            self._market_auth_backoff_until.pop(family, None)
            return raw
        code = response.get("code")
        if code in {"no_session", "auth_required", "auth_expired"}:
            return None
        self._market_auth_backoff_until[family] = (
            monotonic() + _MARKET_AUTH_FAILURE_BACKOFF_SECONDS
        )
        return None

    async def order_depth_snapshot(
        self, order_book_id: str, max_levels: int = 10
    ) -> dict[str, Any]:
        """Read one live multi-level order-depth snapshot in the isolated worker."""
        self._ensure_open()
        if (
            not order_book_id
            or not order_book_id.isascii()
            or not order_book_id.isdecimal()
        ):
            raise AuthWorkerOperationError("Invalid order_book_id")
        if (
            not isinstance(max_levels, int)
            or isinstance(max_levels, bool)
            or not 1 <= max_levels <= 50
        ):
            raise AuthWorkerOperationError("Invalid max_levels")

        command = {
            "action": "order_depth",
            "order_book_id": order_book_id,
            "max_levels": max_levels,
        }
        async with self._operation_lock:
            if self.mode == "persistent":
                if self._persistent_disconnect_active():
                    raise AuthWorkerOperationError("Disconnect confirmation is pending")
                response = await self._persistent_market_command(command)
            else:
                async with self._daemon_lock:
                    process = self._live_daemon()
                    if process is None:
                        raise AuthWorkerRequired
                    status_response = await self._command(process, {"action": "status"})
                    status = self._status_from_response(status_response)
                    if status.state == "awaiting_disconnect":
                        raise AuthWorkerOperationError(
                            "Disconnect confirmation is pending"
                        )
                    if status.state != "connected":
                        raise AuthWorkerRequired
                    try:
                        response = await self._command(process, command)
                    except asyncio.CancelledError:
                        if self._daemon is process:
                            self._daemon = None
                        await self._stop_process(process)
                        raise
        result = self._result_from_response(response)
        if not isinstance(result, dict):
            raise AuthWorkerOperationError("Invalid order-depth result from worker")
        return result

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        await self._stop_market_daemon()

        async with self._daemon_lock:
            process = self._live_daemon()
            if process is not None:
                try:
                    await self._command(process, {"action": "shutdown"}, timeout=10.0)
                except AuthWorkerError:
                    # Best-effort graceful logout: process termination below is the
                    # fallback if the worker cannot acknowledge shutdown.
                    pass
                await self._stop_process(process)
            self._daemon = None

        process = self._ui_process
        self._ui_process = None
        self._ui_status = None
        self._ui_action = None
        if process is not None and process.returncode is None:
            await self._stop_process(process)

        reaper = self._ui_reaper
        self._ui_reaper = None
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
                # Cancellation is expected because broker shutdown owns the UI worker.
                pass

    async def _start_persistent_ui(self, action: str) -> AuthStatus:
        if self._ui_process is not None and self._ui_process.returncode is None:
            return self._ui_status or AuthStatus(
                state="error",
                message="Authentication flow is already running.",
                error_code="auth_flow_busy",
            )

        process = await self._spawn("ui", "persistent")
        self._ui_process = process
        self._ui_action = action
        try:
            response = await self._command(
                process, {"action": action}, timeout=_UI_RESPONSE_TIMEOUT
            )
            status = self._status_from_response(response)
        except AuthWorkerError:
            self._ui_process = None
            self._ui_status = None
            self._ui_action = None
            await self._stop_process(process)
            raise

        self._ui_status = status
        self._ui_reaper = asyncio.create_task(self._reap_ui(process))
        return status

    async def _reap_ui(self, process: asyncio.subprocess.Process) -> None:
        try:
            if process.stdout is not None:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    try:
                        response = self._decode_response(line)
                        if response.get("ok") is True and "status" in response:
                            self._ui_status = self._status_from_response(response)
                    except AuthWorkerOperationError:
                        # Ignore malformed late status frames; the worker is already
                        # terminal and no credential-bearing payload is accepted.
                        pass
            await process.wait()
        finally:
            if self._ui_process is process:
                self._ui_process = None
                self._ui_status = None
                self._ui_action = None

    def _persistent_disconnect_active(self) -> bool:
        return (
            self._ui_action == "disconnect"
            and self._ui_process is not None
            and self._ui_process.returncode is None
        )

    async def _persistent_market_command(
        self, command: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._market_daemon_lock:
            process = self._live_market_daemon()
            if process is None:
                process = await self._spawn("market-daemon", "persistent")
                self._market_daemon = process
            try:
                return await self._command(process, command)
            except asyncio.CancelledError:
                if self._market_daemon is process:
                    self._market_daemon = None
                await self._stop_process(process)
                raise
            except AuthWorkerOperationError:
                if self._market_daemon is process:
                    self._market_daemon = None
                await self._stop_process(process)
                raise

    def _live_market_daemon(self) -> asyncio.subprocess.Process | None:
        process = self._market_daemon
        if process is None:
            return None
        if process.returncode is not None:
            self._market_daemon = None
            return None
        return process

    async def _stop_market_daemon(self) -> None:
        async with self._market_daemon_lock:
            process = self._live_market_daemon()
            self._market_daemon = None
            if process is None:
                return
            try:
                await self._command(process, {"action": "shutdown"}, timeout=5.0)
            except AuthWorkerError:
                pass
            await self._stop_process(process)

    async def _ensure_daemon_locked(self) -> asyncio.subprocess.Process:
        process = self._live_daemon()
        if process is not None:
            return process
        process = await self._spawn("daemon", self.mode)
        self._daemon = process
        return process

    def _live_daemon(self) -> asyncio.subprocess.Process | None:
        process = self._daemon
        if process is None:
            return None
        if process.returncode is not None:
            self._daemon = None
            return None
        return process

    async def _run_once(self, command: dict[str, Any]) -> dict[str, Any]:
        process = await self._spawn("once", "persistent")
        try:
            response = await self._command(process, command)
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                await self._stop_process(process)
            return response
        finally:
            if process.returncode is None:
                await self._stop_process(process)

    async def _spawn(
        self, kind: str, mode: SessionMode
    ) -> asyncio.subprocess.Process:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "avanza_mcp.auth.worker",
                "--kind",
                kind,
                "--mode",
                mode,
                "--parent-pid",
                str(os.getpid()),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=_PIPE_LIMIT,
                creationflags=creationflags,
            )
        except (OSError, ValueError) as error:
            raise AuthWorkerOperationError("Could not start isolated auth worker") from error

    async def _command(
        self,
        process: asyncio.subprocess.Process,
        payload: dict[str, Any],
        *,
        timeout: float = _COMMAND_TIMEOUT,
    ) -> dict[str, Any]:
        if process.returncode is not None or process.stdin is None or process.stdout is None:
            raise AuthWorkerOperationError("Auth worker is unavailable")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            process.stdin.write(data)
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except (BrokenPipeError, ConnectionError, TimeoutError) as error:
            raise AuthWorkerOperationError("Auth worker did not respond") from error
        if not line:
            raise AuthWorkerOperationError("Auth worker closed unexpectedly")
        return self._decode_response(line)

    @staticmethod
    def _decode_response(line: bytes) -> dict[str, Any]:
        if len(line) > _PIPE_LIMIT:
            raise AuthWorkerOperationError("Auth worker response was too large")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AuthWorkerOperationError("Auth worker returned invalid data") from error
        if not isinstance(value, dict) or _contains_forbidden_key(value):
            raise AuthWorkerOperationError("Auth worker returned unsafe data")
        return value

    @staticmethod
    def _status_from_response(response: dict[str, Any]) -> AuthStatus:
        if response.get("ok") is not True:
            code = str(response.get("code") or "worker_error")
            return AuthStatus(
                state="error",
                message="Authentication worker failed safely.",
                error_code=code,
            )
        try:
            return AuthStatus.model_validate(response["status"])
        except Exception as error:
            raise AuthWorkerOperationError("Invalid auth status from worker") from error

    @staticmethod
    def _result_from_response(response: dict[str, Any]) -> Any:
        if response.get("ok") is True and "result" in response:
            return response["result"]
        code = response.get("code")
        if code in {"no_session", "auth_required"}:
            raise AuthWorkerRequired
        if code == "auth_expired":
            raise AuthWorkerExpired
        raise AuthWorkerOperationError(str(code or "worker_error"))

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    def _ensure_open(self) -> None:
        if self._closed:
            raise AuthWorkerOperationError("Auth broker is closed")
