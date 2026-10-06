"""Unit tests for the isolated Placera Forum client."""

from __future__ import annotations

import httpx

from avanza_mcp.client.bankid import CollectStatus
from avanza_mcp.client.forum import ForumAPIClient, ForumBankIDClient


async def test_forum_bankid_flow_uses_verified_paths_and_keeps_token_internal():
    calls = []
    collect_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal collect_count
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert request.headers["x-app-platform"] == "web"

        if request.url.path == "/v1/auth/bankid/start":
            assert request.method == "POST"
            assert request.content == b'{"same_device":false,"scope":"read write beta"}'
            return httpx.Response(200, json={"order_ref": "order-1"})

        if request.url.path == "/v1/auth/bankid/qr":
            assert request.method == "GET"
            assert request.url.params["order_ref"] == "order-1"
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\nsynthetic",
                headers={"Content-Type": "image/png"},
            )

        if request.url.path == "/v1/auth/bankid/collect":
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
        '"instrument":"instrument-1","company":"company-1"}'
    )
