"""Placera Forum API client isolated from Avanza banking session material."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .. import __version__
from .bankid import (
    BankIDError,
    BankIDErrorCode,
    CollectResult,
    CollectStatus,
    SessionMaterial,
)

_ORIGIN = "https://api.forum.placera.se"
_MAX_JSON_BYTES = 256 * 1024
_MAX_QR_BYTES = 256 * 1024


class ForumError(RuntimeError):
    """Safe Placera Forum error without response bodies or credentials."""

    def __init__(self, code: str, *, upstream_status: int | None = None) -> None:
        self.code = code
        self.upstream_status = upstream_status
        suffix = f"_http_{upstream_status}" if upstream_status is not None else ""
        super().__init__(f"placera_forum_{code}{suffix}")


@dataclass(frozen=True)
class ForumTarget:
    instrument_id: str
    instrument_name: str
    instrument_slug: str | None
    company_id: str | None
    company_name: str | None
    company_slug: str | None


@dataclass(frozen=True)
class ForumPostReceipt:
    post_id: str
    instrument_name: str
    instrument_slug: str | None
    company_name: str | None
    company_slug: str | None


def _required_string(value: Any, key: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, dict):
        raise ForumError("malformed_response")
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > max_length:
        raise ForumError("malformed_response")
    return item


class ForumBankIDClient:
    """One Placera Forum BankID attempt.

    The resulting forum bearer token is wrapped in SessionMaterial only so the existing
    isolated browser-auth state machine can retain it without exposing it to the MCP
    process. No Avanza cookies are present.
    """

    def __init__(
        self,
        *,
        request_timeout: float = 20.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            headers={
                "Accept": "application/json",
                "X-APP-PLATFORM": "web",
                "User-Agent": f"avanza-mcp/{__version__}",
            },
            timeout=httpx.Timeout(request_timeout),
            follow_redirects=False,
            trust_env=False,
            transport=_transport,
        )
        self._order_ref: str | None = None
        self._closed = False

    async def start(self) -> str:
        if self._order_ref is not None:
            raise BankIDError(BankIDErrorCode.PROTOCOL)
        body = await self._json_request(
            "POST",
            "/v1/auth/bankid/start",
            json={"same_device": False, "scope": "read write beta"},
        )
        try:
            self._order_ref = _required_string(body, "order_ref", max_length=256)
        except ForumError:
            raise BankIDError(BankIDErrorCode.MALFORMED_RESPONSE) from None
        return await self.restart()

    async def restart(self) -> str:
        if self._order_ref is None:
            raise BankIDError(BankIDErrorCode.PROTOCOL)
        response = await self._request(
            "GET",
            "/v1/auth/bankid/qr",
            params={"order_ref": self._order_ref, "t": int(time.time() * 1000)},
        )
        content = response.content
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if (
            not content
            or len(content) > _MAX_QR_BYTES
            or content_type not in {"image/png", "image/svg+xml", "image/jpeg"}
        ):
            raise BankIDError(BankIDErrorCode.MALFORMED_RESPONSE)
        encoded = base64.b64encode(content).decode("ascii")
        return (
            '<img alt="BankID QR code" '
            f'src="data:{content_type};base64,{encoded}" '
            'style="display:block;width:100%;height:auto">'
        )

    async def collect(self) -> CollectResult:
        if self._order_ref is None:
            raise BankIDError(BankIDErrorCode.PROTOCOL)
        body = await self._json_request(
            "POST",
            "/v1/auth/bankid/collect",
            json={"order_ref": self._order_ref},
        )
        status = str(body.get("status") or "").lower()
        token = body.get("token")

        if isinstance(token, str) and token:
            if len(token) > 16 * 1024:
                raise BankIDError(BankIDErrorCode.MALFORMED_RESPONSE)
            return CollectResult(
                CollectStatus.COMPLETE,
                SessionMaterial((), token),
            )
        if status == "pending":
            return CollectResult(CollectStatus.PENDING)
        if status in {"failed", "cancelled", "canceled", "denied"}:
            return CollectResult(CollectStatus.DENIED)
        if status in {"expired", "timeout", "timed_out"}:
            return CollectResult(CollectStatus.TIMED_OUT)
        raise BankIDError(BankIDErrorCode.PROTOCOL)

    async def cancel(self) -> None:
        order_ref = self._order_ref
        self._order_ref = None
        if order_ref is None:
            return
        try:
            await self._json_request(
                "POST",
                "/v1/auth/bankid/cancel",
                json={"order_ref": order_ref},
            )
        except BankIDError:
            # Cancellation is best-effort; the local attempt is already forgotten.
            return

    async def logout(self, session: SessionMaterial | None = None) -> None:
        if session is None or not session._security_token:
            return
        try:
            await self._request(
                "POST",
                "/user/logout",
                headers={"Authorization": f"Bearer {session._security_token}"},
            )
        except BankIDError:
            raise

    async def validate_session(
        self, session: SessionMaterial
    ) -> SessionMaterial | None:
        token = session._security_token
        if not token:
            return None
        try:
            response = await self._request(
                "GET",
                "/user",
                headers={"Authorization": f"Bearer {token}"},
                accepted_statuses={200, 401, 403},
            )
        except BankIDError:
            raise
        return session if response.status_code == 200 else None

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    async def _json_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(method, path, **kwargs)
        if len(response.content) > _MAX_JSON_BYTES:
            raise BankIDError(BankIDErrorCode.MALFORMED_RESPONSE)
        try:
            body = response.json()
        except ValueError:
            raise BankIDError(BankIDErrorCode.MALFORMED_RESPONSE) from None
        if not isinstance(body, dict):
            raise BankIDError(BankIDErrorCode.MALFORMED_RESPONSE)
        return body

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accepted_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        if self._closed:
            raise BankIDError(BankIDErrorCode.PROTOCOL)
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise BankIDError(BankIDErrorCode.NETWORK) from None
        allowed = accepted_statuses or {200, 201, 204}
        if response.status_code not in allowed:
            raise BankIDError(
                BankIDErrorCode.PROTOCOL,
                upstream_status=response.status_code,
            )
        return response


class ForumAPIClient:
    """Authenticated forum writes with an explicit minimal response projection."""

    def __init__(
        self,
        token: str,
        *,
        request_timeout: float = 20.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token or len(token) > 16 * 1024:
            raise ValueError("Invalid forum token")
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-APP-PLATFORM": "web",
                "User-Agent": f"avanza-mcp/{__version__}",
            },
            timeout=httpx.Timeout(request_timeout),
            follow_redirects=False,
            trust_env=False,
            transport=_transport,
        )

    async def __aenter__(self) -> "ForumAPIClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    async def resolve_instrument(self, isin: str) -> ForumTarget:
        if not isinstance(isin, str) or not isin or len(isin) > 32:
            raise ForumError("invalid_isin")
        response = await self._request(
            "GET",
            "/v1/instruments",
            params={"isin": isin, "page_size": 2},
        )
        body = self._json_object(response)
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ForumError("malformed_response")
        exact = [
            item for item in rows
            if isinstance(item, dict) and item.get("isin") == isin
        ]
        if len(exact) != 1:
            raise ForumError("instrument_not_unique")
        item = exact[0]
        instrument_id = _required_string(item, "id", max_length=128)
        instrument_name = _required_string(item, "name", max_length=512)
        company = item.get("company")
        company_id = company_name = company_slug = None
        if isinstance(company, dict):
            raw_id = company.get("id")
            raw_name = company.get("name")
            raw_slug = company.get("slug")
            company_id = raw_id if isinstance(raw_id, str) and raw_id else None
            company_name = raw_name if isinstance(raw_name, str) and raw_name else None
            company_slug = raw_slug if isinstance(raw_slug, str) and raw_slug else None
        raw_slug = item.get("slug")
        return ForumTarget(
            instrument_id=instrument_id,
            instrument_name=instrument_name,
            instrument_slug=raw_slug if isinstance(raw_slug, str) and raw_slug else None,
            company_id=company_id,
            company_name=company_name,
            company_slug=company_slug,
        )

    async def create_post(
        self,
        *,
        isin: str,
        title: str,
        content: str,
    ) -> ForumPostReceipt:
        target = await self.resolve_instrument(isin)
        payload: dict[str, Any] = {
            "title": title,
            "content": content,
            "tags": [],
            "media": [],
            "instrument": target.instrument_id,
        }
        if target.company_id is not None:
            payload["company"] = target.company_id

        response = await self._request("POST", "/posts", json=payload)
        body = self._json_object(response)
        post_id = _required_string(body, "id", max_length=128)
        return ForumPostReceipt(
            post_id=post_id,
            instrument_name=target.instrument_name,
            instrument_slug=target.instrument_slug,
            company_name=target.company_name,
            company_slug=target.company_slug,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ForumError("network") from None
        if response.status_code in {401, 403}:
            raise ForumError("auth_expired", upstream_status=response.status_code)
        if response.status_code not in {200, 201, 204}:
            raise ForumError("request_failed", upstream_status=response.status_code)
        if len(response.content) > _MAX_JSON_BYTES:
            raise ForumError("malformed_response")
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise ForumError("malformed_response") from None
        if not isinstance(body, dict):
            raise ForumError("malformed_response")
        return body
