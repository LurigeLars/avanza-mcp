"""Credential-isolating subprocess broker for authenticated Avanza operations."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any, Literal

import httpx

from ..client.base import _authenticated_market_kind
from .browser import AuthStatus

SessionMode = Literal["persistent", "memory_only", "one_shot"]
SESSION_MODES = frozenset({"persistent", "memory_only", "one_shot"})

_PIPE_LIMIT = 4 * 1024 * 1024
_COMMAND_TIMEOUT = 90.0
_UI_RESPONSE_TIMEOUT = 10.0
_FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "cookies",
        "securitytoken",
        "security_token",
        "authenticationsession",
        "authentication_session",
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
    value = os.environ.get("AVANZA_SESSION_MODE", "persistent").strip().lower()
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
        self._operation_lock = asyncio.Lock()
        self._ui_process: asyncio.subprocess.Process | None = None
        self._ui_status: AuthStatus | None = None
        self._ui_reaper: asyncio.Task[None] | None = None
        self._closed = False

    async def connect(self) -> AuthStatus:
        self._ensure_open()
        if self.mode == "persistent":
            return await self._start_persistent_ui("connect")
        async with self._daemon_lock:
            process = await self._ensure_daemon_locked()
            response = await self._command(process, {"action": "connect"})
            return self._status_from_response(response)

    async def disconnect(self) -> AuthStatus:
        self._ensure_open()
        if self.mode == "persistent":
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
                response = await self._run_once(command)
            else:
                async with self._daemon_lock:
                    process = self._live_daemon()
                    if process is None:
                        raise AuthWorkerRequired
                    response = await self._command(process, command)
            return self._result_from_response(response)

    async def market_request(
        self, method: str, path: str, kwargs: dict[str, Any]
    ) -> httpx.Response | None:
        """Use isolated auth only for the three reviewed realtime stock GET paths."""
        if _authenticated_market_kind(method, path) is None:
            return None
        if self.mode == "one_shot":
            # One-shot mode reserves its authenticated session for one explicit
            # account workflow. Public market tools remain anonymous.
            return None

        command = {
            "action": "market",
            "method": method,
            "path": path,
            "params": kwargs.get("params"),
        }
        try:
            async with self._operation_lock:
                if self.mode == "persistent":
                    response = await self._run_once(command)
                else:
                    async with self._daemon_lock:
                        process = self._live_daemon()
                        if process is None:
                            return None
                        response = await self._command(process, command)
        except AuthWorkerOperationError:
            return httpx.Response(502, content=b"")

        if response.get("ok") is True:
            return httpx.Response(200, json=response.get("result"))
        code = response.get("code")
        if code == "no_session":
            return None
        if code in {"auth_required", "auth_expired"}:
            return httpx.Response(401, content=b"")
        return httpx.Response(502, content=b"")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        async with self._daemon_lock:
            process = self._live_daemon()
            if process is not None:
                try:
                    await self._command(process, {"action": "shutdown"}, timeout=10.0)
                except AuthWorkerError:
                    pass
                await self._stop_process(process)
            self._daemon = None

        process = self._ui_process
        self._ui_process = None
        self._ui_status = None
        if process is not None and process.returncode is None:
            await self._stop_process(process)

        reaper = self._ui_reaper
        self._ui_reaper = None
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
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
        response = await self._command(
            process, {"action": action}, timeout=_UI_RESPONSE_TIMEOUT
        )
        status = self._status_from_response(response)
        self._ui_status = status
        self._ui_reaper = asyncio.create_task(self._reap_ui(process))
        return status

    async def _reap_ui(self, process: asyncio.subprocess.Process) -> None:
        try:
            await process.wait()
        finally:
            if self._ui_process is process:
                self._ui_process = None
                self._ui_status = None

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
