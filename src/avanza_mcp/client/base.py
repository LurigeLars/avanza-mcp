"""Base HTTP client for Avanza API."""

import asyncio
import copy
import json
import logging
import re
import math
import random
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import Cookie
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .. import __version__
from .endpoints import (
    AuthenticatedMarketEndpoint,
    authenticated_public_request_allowed,
    authenticated_public_request_family,
)
from .exceptions import (
    AvanzaAPIError,
    AvanzaAuthError,
    AvanzaNetworkError,
    AvanzaNotFoundError,
    AvanzaRateLimitError,
    AvanzaRetryableError,
    AvanzaTimeoutError,
)

logger = logging.getLogger(__name__)


_AUTHENTICATED_STOCK_MARKET_PATH = re.compile(
    r"^/_api/market-guide/stock/[0-9]+/(?P<kind>quote|orderdepth|trades)$"
)
_AUTHENTICATED_TRADING_CRITICAL_MARKET_DATA_PATH = re.compile(
    r"^/_api/trading-critical/rest/marketdata/[0-9]+$"
)

_AUTH_QUOTE_FIELDS = frozenset(
    {
        "buy",
        "sell",
        "last",
        "highest",
        "lowest",
        "change",
        "changePercent",
        "spread",
        "timeOfLast",
        "totalValueTraded",
        "totalVolumeTraded",
        "updated",
        "volumeWeightedAveragePrice",
        "isRealTime",
    }
)
_AUTH_TRADE_FIELDS = frozenset(
    {"buyer", "seller", "dealTime", "price", "volume", "matchedOnMarket", "cancelled"}
)
_AUTH_ORDER_SIDE_FIELDS = frozenset({"price", "volume", "priceString"})


def _authenticated_market_kind(method: str, path: str) -> str | None:
    """Return the narrowly approved realtime market shape for this request."""
    if method.upper() != "GET":
        return None
    match = _AUTHENTICATED_STOCK_MARKET_PATH.fullmatch(path)
    if match:
        return match.group("kind")
    if _AUTHENTICATED_TRADING_CRITICAL_MARKET_DATA_PATH.fullmatch(path):
        return "marketdata"
    return None


def _project_mapping(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected authenticated market object")
    return {key: value[key] for key in fields if key in value}


def _project_rest_order_depth(value: Any) -> dict[str, Any]:
    """Project nested REST order depth to a credential-free allowlist."""
    root = _project_mapping(
        value,
        frozenset(
            {
                "receivedTime",
                "levels",
                "marketMakerExpected",
                "marketMakerLevelInAsk",
                "marketMakerLevelInBid",
            }
        ),
    )
    levels = root.get("levels", [])
    if not isinstance(levels, list):
        raise ValueError("Expected authenticated order-depth levels")
    projected_levels: list[dict[str, Any]] = []
    for level in levels:
        projected = _project_mapping(level, frozenset({"buySide", "sellSide"}))
        for side in ("buySide", "sellSide"):
            if side in projected and projected[side] is not None:
                projected[side] = _project_mapping(
                    projected[side], _AUTH_ORDER_SIDE_FIELDS
                )
        projected_levels.append(projected)
    root["levels"] = projected_levels
    return root


def _depth_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"Invalid {field}")
    return float(value)


def _project_sse_side(
    level: dict[str, Any], price_field: str, volume_field: str
) -> tuple[float | None, float | None]:
    price = _depth_number(level.get(price_field), price_field)
    volume = _depth_number(level.get(volume_field), volume_field)

    # Avanza's live feed has been observed encoding an absent side as 0/0.
    # Normalize that sentinel to null/null rather than exposing a fictitious order.
    if (price is None or price == 0) and (volume is None or volume == 0):
        return None, None
    if price is None or volume is None or price <= 0 or volume <= 0:
        raise ValueError("Incomplete order-depth side")
    return price, volume


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid {field}")
    if not math.isfinite(value) or value < 0 or int(value) != value:
        raise ValueError(f"Invalid {field}")
    return int(value)


def _optional_market_maker_level(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid {field}")
    if not math.isfinite(value) or int(value) != value:
        raise ValueError(f"Invalid {field}")
    parsed = int(value)
    return parsed if parsed >= 0 else None


def _project_sse_order_depth_payload(
    value: Any, *, order_book_id: str, max_levels: int
) -> dict[str, Any]:
    """Strictly project one flat ORDER_DEPTH SSE snapshot."""
    if not isinstance(value, dict):
        raise ValueError("Expected ORDER_DEPTH object")

    observed_id = value.get("orderbookId")
    if isinstance(observed_id, bool):
        raise ValueError("Invalid orderbookId")
    observed_id = str(observed_id) if observed_id is not None else ""
    if observed_id != order_book_id:
        raise ValueError("ORDER_DEPTH orderbookId mismatch")

    levels = value.get("levels")
    if not isinstance(levels, list):
        raise ValueError("Expected ORDER_DEPTH levels")

    projected_levels: list[dict[str, Any]] = []
    for level in levels[:max_levels]:
        if not isinstance(level, dict):
            raise ValueError("Invalid ORDER_DEPTH level")
        buy_price, buy_volume = _project_sse_side(
            level, "buyPrice", "buyVolume"
        )
        sell_price, sell_volume = _project_sse_side(
            level, "sellPrice", "sellVolume"
        )
        projected_levels.append(
            {
                "buyPrice": buy_price,
                "buyVolume": buy_volume,
                "sellPrice": sell_price,
                "sellVolume": sell_volume,
            }
        )

    return {
        "orderBookId": order_book_id,
        "receivedTime": _optional_nonnegative_int(
            value.get("receivedTime"), "receivedTime"
        ),
        "levels": projected_levels,
        "marketMakerLevelInAsk": _optional_market_maker_level(
            value.get("marketMakerLevelInAsk"), "marketMakerLevelInAsk"
        ),
        "marketMakerLevelInBid": _optional_market_maker_level(
            value.get("marketMakerLevelInBid"), "marketMakerLevelInBid"
        ),
    }


def _project_authenticated_market_payload(kind: str, value: Any) -> Any:
    """Strictly project authenticated market responses to known public fields."""
    if kind == "quote":
        return _project_mapping(value, _AUTH_QUOTE_FIELDS)

    if kind == "marketdata":
        root = _project_mapping(value, frozenset({"quote", "orderDepth"}))
        root["quote"] = _project_mapping(root.get("quote"), _AUTH_QUOTE_FIELDS)
        if "orderDepth" in root:
            root["orderDepth"] = _project_rest_order_depth(root["orderDepth"])
        return root

    if kind == "trades":
        if not isinstance(value, list):
            raise ValueError("Expected authenticated trades array")
        return [_project_mapping(item, _AUTH_TRADE_FIELDS) for item in value]

    if kind == "orderdepth":
        return _project_rest_order_depth(value)

    raise ValueError("Unapproved authenticated market response")



class AuthenticatedSession(Protocol):
    _cookies: tuple[Cookie, ...]
    _security_token: str | None


class AvanzaClient:
    """Async HTTP client for Avanza public API."""

    # Default configuration
    DEFAULT_BASE_URL = "https://www.avanza.se"
    DEFAULT_TIMEOUT = 30.0
    DEFAULT_CONNECT_TIMEOUT = 5.0
    DEFAULT_MAX_CONNECTIONS = 10
    DEFAULT_MAX_KEEPALIVE = 5
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_MAX_IN_FLIGHT_REQUESTS = 4
    DEFAULT_MIN_REQUEST_INTERVAL = 0.125
    DEFAULT_REQUEST_JITTER = 0.025
    DEFAULT_RATE_LIMIT_COOLDOWN = 5.0

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_keepalive_connections: int = DEFAULT_MAX_KEEPALIVE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        request_timeout: float = DEFAULT_TIMEOUT,
        session_provider: Callable[[], AuthenticatedSession | None] | None = None,
        session_invalidated: Callable[[], Awaitable[None]] | None = None,
        authenticated_request_delegate: Callable[
            [str, str, dict[str, Any]], Awaitable[httpx.Response | None]
        ] | None = None,
        authenticated_market_data_batch_delegate: Callable[
            [list[str]], Awaitable[list[dict[str, Any] | None] | None]
        ] | None = None,
        max_in_flight_requests: int = DEFAULT_MAX_IN_FLIGHT_REQUESTS,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        request_jitter: float = DEFAULT_REQUEST_JITTER,
        rate_limit_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN,
    ) -> None:
        """Initialize Avanza client.

        Args:
            base_url: Base URL for Avanza API
            timeout: Read timeout in seconds
            connect_timeout: Connection timeout in seconds
            max_connections: Maximum number of concurrent connections
            max_keepalive_connections: Maximum number of keepalive connections
            max_retries: Maximum total attempts for transient failures
            request_timeout: Overall deadline in seconds, including retries and waits
            max_in_flight_requests: Per-client cap for simultaneous Avanza requests
            min_request_interval: Minimum spacing between new upstream request starts
            request_jitter: Additional random spacing added to each request start
            rate_limit_cooldown: Fallback cooldown after a 429 without Retry-After
        """
        self._base_url = base_url
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._max_connections = max_connections
        self._max_keepalive_connections = max_keepalive_connections
        self._max_retries = max_retries
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout must be positive and finite")
        self._request_timeout = request_timeout
        self._session_provider = session_provider
        self._session_invalidated = session_invalidated
        self._authenticated_request_delegate = authenticated_request_delegate
        self._authenticated_market_data_batch_delegate = (
            authenticated_market_data_batch_delegate
        )
        if (
            not isinstance(max_in_flight_requests, int)
            or isinstance(max_in_flight_requests, bool)
            or max_in_flight_requests < 1
        ):
            raise ValueError("max_in_flight_requests must be a positive integer")
        for name, value in (
            ("min_request_interval", min_request_interval),
            ("request_jitter", request_jitter),
            ("rate_limit_cooldown", rate_limit_cooldown),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        self._max_in_flight_requests = max_in_flight_requests
        self._min_request_interval = min_request_interval
        self._request_jitter = request_jitter
        self._rate_limit_cooldown = rate_limit_cooldown
        self._request_semaphore = asyncio.Semaphore(max_in_flight_requests)
        self._request_pace_lock = asyncio.Lock()
        self._next_request_start = 0.0
        self._rate_limit_until = 0.0
        self._client: httpx.AsyncClient | None = None
        self._authenticated_client: httpx.AsyncClient | None = None
        self._authenticated_session: AuthenticatedSession | None = None
        self._client_lock = asyncio.Lock()

    async def __aenter__(self) -> "AvanzaClient":
        """Initialize httpx client with connection pooling.

        Returns:
            Self for context manager usage
        """
        # Configure timeouts with separate connect and read values
        timeout = httpx.Timeout(
            self._timeout,
            connect=self._connect_timeout,
        )

        # Configure connection pooling limits
        limits = httpx.Limits(
            max_connections=self._max_connections,
            max_keepalive_connections=self._max_keepalive_connections,
        )

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "User-Agent": f"avanza-mcp/{__version__}",
                "Accept": "application/json",
            },
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
        )

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Clean up httpx client.

        Args:
            exc_type: Exception type if an error occurred
            exc_val: Exception value if an error occurred
            exc_tb: Exception traceback if an error occurred
        """
        if self._client:
            await self._client.aclose()
        await self.clear_authenticated_session()

    async def clear_authenticated_session(self) -> None:
        """Drop any client object carrying copied authenticated cookies/token."""
        async with self._client_lock:
            client = self._authenticated_client
            self._authenticated_client = None
            self._authenticated_session = None
        if client is not None:
            # References are already cleared even if transport cleanup itself fails.
            with suppress(Exception):
                await client.aclose()

    def _public_client(self) -> httpx.AsyncClient:
        client = self._client
        if client is None:
            raise RuntimeError("Client not initialized. Use async context manager.")
        return client

    async def _ensure_authenticated_client_locked(
        self, session: AuthenticatedSession
    ) -> httpx.AsyncClient:
        """Return the cached auth client. Caller must hold ``_client_lock``."""
        if session is not self._authenticated_session:
            previous = self._authenticated_client
            self._authenticated_client = None
            self._authenticated_session = None
            if previous is not None:
                await previous.aclose()

            cookies = httpx.Cookies()
            for cookie in session._cookies:
                cookies.jar.set_cookie(copy.copy(cookie))
            headers = {
                "User-Agent": f"avanza-mcp/{__version__}",
                "Accept": "application/json",
            }
            if session._security_token is not None:
                headers["X-SecurityToken"] = session._security_token
            self._authenticated_client = httpx.AsyncClient(
                base_url=self._base_url,
                cookies=cookies,
                headers=headers,
                timeout=httpx.Timeout(self._timeout, connect=self._connect_timeout),
                limits=httpx.Limits(
                    max_connections=self._max_connections,
                    max_keepalive_connections=self._max_keepalive_connections,
                ),
                follow_redirects=False,
                trust_env=False,
            )
            self._authenticated_session = session

        client = self._authenticated_client
        if client is None:
            raise AvanzaAuthError("No authenticated Avanza session is available")
        return client

    @staticmethod
    def _require_same_origin_authenticated_path(path: str) -> None:
        """Reject absolute, protocol-relative, query-bearing, or fragment paths."""
        parsed = urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.path != path
        ):
            raise AvanzaAuthError(
                "Authenticated Avanza requests require a relative same-origin path"
            )

    async def _pace_request_start(self) -> None:
        """Apply per-client pacing and any active 429 cooldown before a request."""

        loop = asyncio.get_running_loop()
        async with self._request_pace_lock:
            while True:
                now = loop.time()
                not_before = max(self._next_request_start, self._rate_limit_until)
                delay = not_before - now
                if delay <= 0:
                    break
                await asyncio.sleep(delay)

            jitter = (
                random.uniform(0.0, self._request_jitter)
                if self._request_jitter
                else 0.0
            )
            self._next_request_start = (
                loop.time() + self._min_request_interval + jitter
            )

    def _note_rate_limit(self, retry_after: int | None) -> None:
        """Delay subsequent request starts after an upstream 429 response."""

        delay = (
            float(retry_after)
            if retry_after is not None
            else self._rate_limit_cooldown
        )
        loop = asyncio.get_running_loop()
        self._rate_limit_until = max(
            self._rate_limit_until,
            loop.time() + max(0.0, delay),
        )

    async def _send_request(
        self,
        method: str,
        path: str,
        *,
        allow_authenticated: bool,
        require_authenticated: bool = False,
        **kwargs: Any,
    ) -> tuple[httpx.Response, bool]:
        """Send one governed upstream request."""

        async with self._request_semaphore:
            await self._pace_request_start()
            return await self._send_request_unpaced(
                method,
                path,
                allow_authenticated=allow_authenticated,
                require_authenticated=require_authenticated,
                **kwargs,
            )

    async def _send_request_unpaced(
        self,
        method: str,
        path: str,
        *,
        allow_authenticated: bool,
        require_authenticated: bool = False,
        **kwargs: Any,
    ) -> tuple[httpx.Response, bool]:
        """Send one request while serializing auth selection through completion.

        The client lock intentionally spans an authenticated network request. This
        makes disconnect/clear wait for already-started requests, while any request
        queued behind disconnect observes the cleared session and cannot resurrect
        a stale authenticated client.
        """
        public_client = self._public_client()
        delegate_possible = (
            allow_authenticated
            and self._authenticated_request_delegate is not None
            and self._base_url.rstrip("/") == self.DEFAULT_BASE_URL
        )
        if delegate_possible:
            delegated = await self._authenticated_request_delegate(
                method,
                path,
                dict(kwargs),
            )
            if delegated is not None:
                return delegated, True
            if require_authenticated:
                raise AvanzaAuthError("No authenticated Avanza session is available")

        auth_possible = (
            allow_authenticated
            and self._session_provider is not None
            and self._base_url.rstrip("/") == self.DEFAULT_BASE_URL
        )
        if not auth_possible:
            if require_authenticated:
                raise AvanzaAuthError("No authenticated Avanza session is available")
            return await public_client.request(method, path, **kwargs), False

        async with self._client_lock:
            session = self._session_provider()
            if session is None:
                if require_authenticated:
                    raise AvanzaAuthError("No authenticated Avanza session is available")
            else:
                auth_client = await self._ensure_authenticated_client_locked(session)
                response = await auth_client.request(method, path, **kwargs)
                return response, True

        # No authenticated session exists. Public market requests may continue
        # anonymously; privileged account requests use require_authenticated=True.
        return await public_client.request(method, path, **kwargs), False

    async def request_authenticated(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send one same-origin request through the current authenticated session."""
        self._require_same_origin_authenticated_path(path)
        response, authenticated = await self._send_request(
            method,
            path,
            allow_authenticated=True,
            require_authenticated=True,
            **kwargs,
        )
        if not authenticated:  # Defensive; require_authenticated already enforces this.
            raise AvanzaAuthError("No authenticated Avanza session is available")
        return response


    async def request_authenticated_batch(
        self,
        paths: list[str],
        *,
        max_concurrency: int = 8,
    ) -> list[httpx.Response | None]:
        """Send a bounded GET batch through one stable authenticated client lease.

        The lifecycle lock is held for the whole batch so disconnect/clear cannot close
        or replace the authenticated client mid-flight. Requests inside that lease may
        run concurrently, while the normal per-client pacing still governs request
        starts. Result order always matches the input path order.
        """
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if not paths:
            return []
        for path in paths:
            self._require_same_origin_authenticated_path(path)

        auth_possible = (
            self._session_provider is not None
            and self._base_url.rstrip("/") == self.DEFAULT_BASE_URL
        )
        if not auth_possible:
            raise AvanzaAuthError("No authenticated Avanza session is available")

        async with self._client_lock:
            session = self._session_provider()
            if session is None:
                raise AvanzaAuthError("No authenticated Avanza session is available")
            auth_client = await self._ensure_authenticated_client_locked(session)
            semaphore = asyncio.Semaphore(max_concurrency)

            async def fetch_one(path: str) -> httpx.Response | None:
                async with semaphore:
                    async with self._request_semaphore:
                        await self._pace_request_start()
                        try:
                            return await auth_client.get(path)
                        except httpx.HTTPError:
                            return None

            return list(await asyncio.gather(*(fetch_one(path) for path in paths)))

    def _handle_error(
        self,
        response: httpx.Response,
        path: str,
        request_id: str,
        params: dict[str, Any] | None = None,
        *,
        authenticated: bool = False,
    ) -> None:
        """Handle HTTP error responses with enhanced context.

        Args:
            response: HTTP response object
            path: Request path for error context
            request_id: Request ID for debugging
            params: Query parameters for error context

        Raises:
            AvanzaNotFoundError: If resource not found (404)
            AvanzaAuthError: If authentication failed (401, 403)
            AvanzaRateLimitError: If rate limit exceeded (429)
            AvanzaAPIError: For other API errors
        """
        status_code = response.status_code

        if authenticated:
            # Never inspect or log authenticated upstream error bodies: Avanza may
            # include account/session metadata in failure responses.
            error_data = None
            message = f"HTTP {status_code}"
            context = f"[{request_id}] {path}"
            logger.warning(
                "Authenticated API error: status=%d path=%s request_id=%s",
                status_code,
                path,
                request_id,
            )
        else:
            try:
                error_data = response.json()
            except ValueError:
                error_data = None
            message = (
                error_data.get("message", response.text)
                if isinstance(error_data, dict)
                else response.text
            )
            message = str(message if message is not None else f"HTTP {status_code}")
            message = message[:500] or f"HTTP {status_code}"

            context = f"[{request_id}] {path}"
            if params:
                context += f" params={params}"

            logger.warning(
                "API error: status=%d path=%s request_id=%s message=%s",
                status_code,
                path,
                request_id,
                message[:200],
            )

        # Handle specific error types
        if status_code == 404:
            raise AvanzaNotFoundError(f"{context}: {message}")
        elif status_code in (401, 403):
            raise AvanzaAuthError(f"{context}: {message}")
        elif status_code == 429:
            retry_after = response.headers.get("Retry-After", "").strip()
            seconds = None
            try:
                if retry_after.isascii() and retry_after.isdecimal():
                    seconds = int(retry_after)
                elif retry_after:
                    date = parsedate_to_datetime(retry_after)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    seconds = max(
                        0,
                        math.ceil((date - datetime.now(timezone.utc)).total_seconds()),
                    )
            except (ValueError, TypeError, OverflowError):
                # Invalid Retry-After values are treated as unspecified.
                pass
            self._note_rate_limit(seconds)
            raise AvanzaRateLimitError(seconds, f"{context}: {message}")
        else:
            error_type = (
                AvanzaRetryableError if 500 <= status_code < 600 else AvanzaAPIError
            )
            raise error_type(
                status_code,
                f"{context}: {message}",
                error_data if isinstance(error_data, (dict, list)) else None,
            )

    async def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """GET request with retry logic, error handling, and JSON parsing.

        Automatically retries on transient failures (network errors, timeouts,
        server errors) with jittered exponential backoff, excluding pool timeouts.

        Args:
            path: API endpoint path
            params: Optional query parameters

        Returns:
            JSON response as an object or array

        Raises:
            AvanzaError: If request fails after all retries
        """
        return await self._request("GET", path, params=params)

    async def get_authenticated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """GET one reviewed read-only endpoint and require the authenticated path."""
        self._require_same_origin_authenticated_path(path)
        if not authenticated_public_request_allowed("GET", path):
            raise AvanzaAuthError("Authenticated market path is not approved")
        return await self._request(
            "GET",
            path,
            params=params,
            require_authenticated=True,
        )

    async def get_authenticated_order_depth_snapshot(
        self,
        order_book_id: str,
        *,
        max_levels: int = 10,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        """Read the first valid ORDER_DEPTH SSE snapshot through the auth session."""
        if (
            not order_book_id
            or not order_book_id.isascii()
            or not order_book_id.isdecimal()
        ):
            raise ValueError("Order-book id must contain only ASCII numeric digits")
        if (
            not isinstance(max_levels, int)
            or isinstance(max_levels, bool)
            or not 1 <= max_levels <= 50
        ):
            raise ValueError("max_levels must be an integer between 1 and 50")
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 10:
            raise ValueError("timeout must be finite and between 0 and 10 seconds")

        path = AuthenticatedMarketEndpoint.ORDER_DEPTH_PUSH.format(id=order_book_id)
        self._require_same_origin_authenticated_path(path)
        if (
            self._session_provider is None
            or self._base_url.rstrip("/") != self.DEFAULT_BASE_URL
        ):
            raise AvanzaAuthError("No authenticated Avanza session is available")

        headers = {
            "Accept": "text/event-stream",
            "Accept-Language": "en-US,en;q=0.6",
            "aza-do-not-touch-session": "true",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Pragma": "no-cache",
            "Referer": (
                f"https://www.avanza.se/handla/order.html/kop/{order_book_id}"
            ),
        }

        def parse_snapshot(event: str, data_lines: list[str]) -> dict[str, Any] | None:
            if event != "ORDER_DEPTH":
                return None
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError as error:
                raise ValueError("Malformed ORDER_DEPTH JSON") from error
            return _project_sse_order_depth_payload(
                payload,
                order_book_id=order_book_id,
                max_levels=max_levels,
            )

        async with self._request_semaphore:
            await self._pace_request_start()
            async with self._client_lock:
                session = self._session_provider()
                if session is None:
                    raise AvanzaAuthError(
                        "No authenticated Avanza session is available"
                    )
                auth_client = await self._ensure_authenticated_client_locked(session)

                try:
                    async with asyncio.timeout(timeout):
                        async with auth_client.stream(
                            "GET", path, headers=headers
                        ) as response:
                            if response.status_code in (401, 403):
                                if self._session_invalidated is not None:
                                    await self._session_invalidated()
                                raise AvanzaAuthError(
                                    "Authenticated order-depth stream was denied"
                                )
                            if response.status_code == 429:
                                self._note_rate_limit(None)
                                raise AvanzaRateLimitError(
                                    None, "Authenticated order-depth stream rate limited"
                                )
                            if response.status_code != 200:
                                raise AvanzaAPIError(
                                    response.status_code,
                                    "Authenticated order-depth stream failed",
                                )

                            event = ""
                            data_lines: list[str] = []
                            async for line in response.aiter_lines():
                                if line == "":
                                    snapshot = parse_snapshot(event, data_lines)
                                    if snapshot is not None:
                                        return snapshot
                                    event = ""
                                    data_lines = []
                                    continue
                                if line.startswith(":"):
                                    continue
                                field, separator, raw_value = line.partition(":")
                                if not separator:
                                    raw_value = ""
                                elif raw_value.startswith(" "):
                                    raw_value = raw_value[1:]
                                if field == "event":
                                    event = raw_value.strip()
                                elif field == "data":
                                    data_lines.append(raw_value)

                            snapshot = parse_snapshot(event, data_lines)
                            if snapshot is not None:
                                return snapshot
                except TimeoutError as error:
                    raise AvanzaTimeoutError(
                        "Order-depth stream timed out before ORDER_DEPTH"
                    ) from error
                except httpx.TimeoutException as error:
                    raise AvanzaTimeoutError(
                        "Order-depth stream timed out before ORDER_DEPTH"
                    ) from error
                except httpx.HTTPError as error:
                    raise AvanzaNetworkError(
                        "Order-depth stream failed before ORDER_DEPTH"
                    ) from error

        raise AvanzaNetworkError(
            "Order-depth stream closed before an ORDER_DEPTH snapshot"
        )

    async def get_authenticated_market_data_batch(
        self, paths: list[str]
    ) -> list[dict[str, Any] | None]:
        """GET reviewed trading-critical market-data paths in one auth-worker batch."""
        if not paths:
            return []
        for path in paths:
            self._require_same_origin_authenticated_path(path)
            if (
                authenticated_public_request_family("GET", path)
                != "trading_critical_market_data"
            ):
                raise AvanzaAuthError(
                    "Authenticated batch path is not approved trading-critical market data"
                )

        delegate = self._authenticated_market_data_batch_delegate
        if (
            delegate is not None
            and self._base_url.rstrip("/") == self.DEFAULT_BASE_URL
        ):
            result = await delegate(list(paths))
            if result is None:
                raise AvanzaAuthError("No authenticated Avanza session is available")
            if len(result) != len(paths):
                raise AvanzaAPIError(
                    502,
                    "Authenticated market-data batch returned an invalid result count",
                )
            if any(item is not None and not isinstance(item, dict) for item in result):
                raise AvanzaAPIError(
                    502,
                    "Authenticated market-data batch returned an invalid result shape",
                )
            return result

        results: list[dict[str, Any] | None] = []
        for path in paths:
            try:
                item = await self.get_authenticated(path)
            except (AvanzaNotFoundError, AvanzaAPIError):
                results.append(None)
                continue
            if not isinstance(item, dict):
                raise AvanzaAPIError(
                    502,
                    "Authenticated market-data batch returned an invalid result shape",
                )
            results.append(item)
        return results

    async def get_public(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """Send one read-only GET without reusing authenticated session state."""
        return await self._request(
            "GET",
            path,
            params=params,
            allow_authenticated_public=False,
        )

    async def post(
        self, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """POST request with retry logic, error handling, and JSON parsing.

        Automatically retries on transient failures (network errors, timeouts,
        server errors) with jittered exponential backoff, excluding pool timeouts.
        Intended for Avanza's read-only search/filter POST endpoints; do not use
        automatic retries for non-idempotent operations.

        Args:
            path: API endpoint path
            json: Optional JSON body

        Returns:
            JSON response as an object or array

        Raises:
            AvanzaError: If request fails after all retries
        """
        return await self._request("POST", path, json=json)

    async def post_public(
        self, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """Send one read-only POST without reusing authenticated session state.

        Use this only for endpoints whose response does not need login state. It
        shares the normal public connection pool, retry policy, validation, and
        error handling while bypassing the authenticated delegate/client lock.
        """
        return await self._request(
            "POST",
            path,
            json=json,
            allow_authenticated_public=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        allow_authenticated_public: bool = True,
        require_authenticated: bool = False,
    ) -> dict[str, Any] | list[Any]:
        """Send a request through the shared retry and response pipeline."""
        request_id = str(uuid.uuid4())[:8]
        post_prefix = "POST " if method == "POST" else ""

        auth_market_kind = _authenticated_market_kind(method, path)
        auth_public_allowed = (
            allow_authenticated_public
            and authenticated_public_request_allowed(method, path)
        )
        allow_authenticated = require_authenticated or auth_public_allowed

        @retry(
            retry=retry_if_exception(
                lambda exc: (
                    isinstance(
                        exc,
                        (AvanzaTimeoutError, AvanzaNetworkError, AvanzaRetryableError),
                    )
                    and not isinstance(exc.__cause__, httpx.PoolTimeout)
                )
            ),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=2, max=10),
            reraise=True,
            before_sleep=lambda retry_state: logger.info(
                f"Retrying {post_prefix}request [%s] %s, attempt %d after %s",
                request_id,
                path,
                retry_state.attempt_number,
                type(retry_state.outcome.exception()).__name__
                if retry_state.outcome
                else "unknown",
            ),
        )
        async def _request_with_retry() -> dict[str, Any] | list[Any]:
            try:
                response, authenticated = await self._send_request(
                    method,
                    path,
                    allow_authenticated=allow_authenticated,
                    require_authenticated=require_authenticated,
                    params=params,
                    json=json,
                )
                if (
                    authenticated
                    and response.status_code == 401
                    and self._session_invalidated is not None
                ):
                    # Never replace expired authenticated data with anonymous/delayed data.
                    await self._session_invalidated()
            except httpx.TimeoutException as e:
                logger.warning(
                    "POST timeout [%s] %s: %s"
                    if method == "POST"
                    else "Request timeout [%s] %s: %s",
                    request_id,
                    path,
                    str(e),
                )
                raise AvanzaTimeoutError(
                    f"[{request_id}] {type(e).__name__}: {path}"
                ) from e
            except (httpx.NetworkError, httpx.RemoteProtocolError) as e:
                logger.warning(
                    "POST network error [%s] %s: %s"
                    if method == "POST"
                    else "Network error [%s] %s: %s",
                    request_id,
                    path,
                    str(e),
                )
                raise AvanzaNetworkError(
                    f"[{request_id}] Network error: {path} - {str(e)}"
                ) from e

            if not response.is_success:
                self._handle_error(
                    response, path, request_id, params, authenticated=authenticated
                )

            # Handle empty responses
            if not response.content:
                raise AvanzaAPIError(
                    response.status_code, f"[{request_id}] Empty JSON response: {path}"
                )

            # Parse JSON response
            try:
                data = response.json()
                if not isinstance(data, (dict, list)):
                    raise ValueError("Expected a JSON object or array")
                if authenticated and auth_market_kind is not None:
                    data = _project_authenticated_market_payload(auth_market_kind, data)
                return data
            except ValueError as e:
                logger.error(
                    f"{post_prefix}JSON parse error [%s] %s: %s",
                    request_id,
                    path,
                    str(e),
                )
                raise AvanzaAPIError(
                    response.status_code,
                    f"[{request_id}] Invalid JSON response: {path}",
                ) from e

        if method == "POST":
            logger.debug("POST [%s] %s", request_id, path)
        else:
            logger.debug("GET [%s] %s params=%s", request_id, path, params)
        try:
            async with asyncio.timeout(self._request_timeout):
                return await _request_with_retry()
        except TimeoutError as e:
            raise AvanzaTimeoutError(
                f"[{request_id}] Request deadline exceeded after {self._request_timeout}s: {path}"
            ) from e
        except AvanzaRetryableError as e:
            raise AvanzaAPIError(e.status_code, e.message, e.response) from e
