"""Unit tests for the isolated Placera Forum client."""

from __future__ import annotations

import httpx
import pytest

from avanza_mcp.client.bankid import CollectStatus
from avanza_mcp.client.forum import ForumAPIClient, ForumBankIDClient, ForumError


async def test_forum_bankid_flow_uses_verified_paths_and_keeps_token_internal():
    calls = []
    collect_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal collect_count
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert request.headers["x-app-platform"] == "web"

        if request.url.path == "/v1/auth/bankid/start":
            assert request.method == "POST"
            assert request.headers["accept"] == "application/json"
            assert request.content == b'{"same_device":false,"scope":"read write beta"}'
            return httpx.Response(200, json={"order_ref": "order-1"})

        if request.url.path == "/v1/auth/bankid/qr":
            assert request.method == "GET"
            assert request.headers["accept"] == "*/*"
            assert request.url.params["order_ref"] == "order-1"
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\nsynthetic",
                headers={"Content-Type": "image/png"},
            )

        if request.url.path == "/v1/auth/bankid/collect":
            assert request.headers["accept"] == "application/json"
            collect_count += 1
            if collect_count == 1:
                return httpx.Response(
                    200,
                    json={
                        "status": "pending",
                        "hintCode": "outstandingTransaction",
                        "orderRef": "order-1",
                        "token": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "complete",
                    "hintCode": None,
                    "orderRef": "order-1",
                    "token": "forum-secret-token",
                },
            )

        if request.url.path == "/v1/auth/bankid/cancel":
            return httpx.Response(200, json={})

        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = ForumBankIDClient(_transport=httpx.MockTransport(handler))
    try:
        qr_markup = await client.start()
        assert "data:image/png;base64," in qr_markup

        pending = await client.collect()
        assert pending.status is CollectStatus.PENDING
        assert pending.session is None

        complete = await client.collect()
        assert complete.status is CollectStatus.COMPLETE
        assert complete.session is not None
        assert complete.session._security_token == "forum-secret-token"
        assert "forum-secret-token" not in repr(complete)
    finally:
        await client.aclose()

    assert calls[0][:2] == ("POST", "/v1/auth/bankid/start")
    assert calls[1][:2] == ("GET", "/v1/auth/bankid/qr")


async def test_forum_post_maps_isin_and_sends_exact_frontend_payload():
    seen_post = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_post
        assert request.headers["authorization"] == "Bearer forum-token"
        assert request.headers["x-app-platform"] == "web"

        if request.method == "GET" and request.url.path == "/v1/instruments":
            assert request.url.params["isin"] == "SE0000115446"
            assert request.url.params["page_size"] == "2"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "instrument-1",
                            "isin": "SE0000115446",
                            "name": "Volvo B",
                            "slug": "volvo-b",
                            "company": {
                                "id": "company-1",
                                "name": "Volvo",
                                "slug": "volvo",
                            },
                        }
                    ]
                },
            )

        if request.method == "POST" and request.url.path == "/posts":
            seen_post = request.read().decode("utf-8")
            return httpx.Response(201, json={"id": "post-1"})

        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with ForumAPIClient(
        "forum-token",
        _transport=httpx.MockTransport(handler),
    ) as client:
        receipt = await client.create_post(
            isin="SE0000115446",
            title="Title",
            content="Exact body",
        )

    assert receipt.post_id == "post-1"
    assert receipt.instrument_name == "Volvo B"
    assert receipt.company_slug == "volvo"
    assert seen_post == (
        '{"title":"Title","content":"Exact body","tags":[],"media":[],'
        '"company":"company-1"}'
    )


async def test_forum_post_uses_instrument_when_no_company_exists():
    seen_post = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_post

        if request.method == "GET" and request.url.path == "/v1/instruments":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "instrument-1",
                            "isin": "SE0000000001",
                            "name": "Synthetic",
                            "slug": "synthetic",
                            "company": None,
                        }
                    ]
                },
            )

        if request.method == "POST" and request.url.path == "/posts":
            seen_post = request.read().decode("utf-8")
            return httpx.Response(201, json={"id": "post-1"})

        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with ForumAPIClient(
        "forum-token",
        _transport=httpx.MockTransport(handler),
    ) as client:
        await client.create_post(
            isin="SE0000000001",
            title="",
            content="Exact body",
        )

    assert seen_post == (
        '{"title":"","content":"Exact body","tags":[],"media":[],'
        '"instrument":"instrument-1"}'
    )


async def test_forum_resolver_prefers_company_primary_instrument_for_duplicate_isin():
    isin = "US91913Y1001"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/instruments"
        assert request.url.params["isin"] == isin
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "v1l-xetra",
                        "isin": isin,
                        "name": "Valero Energy",
                        "slug": "valero-energy",
                        "symbol": "V1L",
                        "company": {
                            "id": "valero-company",
                            "name": "Valero Energy",
                            "slug": "valero-energy",
                            "primary_instrument": {
                                "id": "vlo-primary",
                                "isin": isin,
                                "symbol": "VLO",
                            },
                        },
                    },
                    {
                        "id": "vlo-primary",
                        "isin": isin,
                        "name": "Valero Energy",
                        "slug": "valero-energy",
                        "symbol": "VLO",
                        "company": {
                            "id": "valero-company",
                            "name": "Valero Energy",
                            "slug": "valero-energy",
                            "primary_instrument": {
                                "id": "vlo-primary",
                                "isin": isin,
                                "symbol": "VLO",
                            },
                        },
                    },
                ]
            },
        )

    async with ForumAPIClient(
        "forum-token",
        _transport=httpx.MockTransport(handler),
    ) as client:
        target = await client.resolve_instrument(isin)

    assert target.instrument_id == "vlo-primary"
    assert target.instrument_name == "Valero Energy"
    assert target.company_id == "valero-company"


async def test_forum_resolver_still_fails_closed_when_duplicate_isin_has_no_unique_primary():
    isin = "US0000000001"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "listing-a",
                        "isin": isin,
                        "name": "Synthetic",
                        "company": {"id": "company-a", "name": "Synthetic"},
                    },
                    {
                        "id": "listing-b",
                        "isin": isin,
                        "name": "Synthetic",
                        "company": {"id": "company-b", "name": "Synthetic"},
                    },
                ]
            },
        )

    async with ForumAPIClient(
        "forum-token",
        _transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ForumError, match="instrument_not_unique"):
            await client.resolve_instrument(isin)
