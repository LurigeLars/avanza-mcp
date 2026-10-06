"""Temporary public Placera auth probe. Remove before merge."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


BASE = "https://api.forum.placera.se"


def _request(path: str, *, method: str = "GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "User-Agent": "avanza-mcp-contract-probe/1.0",
            "X-APP-PLATFORM": "web",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    return urllib.request.urlopen(request, timeout=20)


def test_public_placera_bankid_probe():
    with _request(
        "/v1/auth/bankid/start",
        method="POST",
        payload={"same_device": False, "scope": "read write beta"},
    ) as response:
        start = json.loads(response.read().decode("utf-8"))
    order_ref = start["order_ref"]

    try:
        query = urllib.parse.urlencode({"order_ref": order_ref, "t": 1})
        with _request("/v1/auth/bankid/qr?" + query) as response:
            qr_type = response.headers.get("Content-Type")
            qr = response.read(64)

        with _request(
            "/v1/auth/bankid/collect",
            method="POST",
            payload={"order_ref": order_ref},
        ) as response:
            collect = json.loads(response.read().decode("utf-8"))

        safe = {
            "start_keys": sorted(start),
            "qr_content_type": qr_type,
            "qr_prefix_hex": qr[:32].hex(),
            "collect_keys": sorted(collect),
            "collect_status": collect.get("status"),
            "collect_hint_code": collect.get("hintCode"),
            "collect_has_token": isinstance(collect.get("token"), str),
        }
        raise AssertionError("PLACERA_RUNTIME_PROBE=" + repr(safe))
    finally:
        try:
            with _request(
                "/v1/auth/bankid/cancel",
                method="POST",
                payload={"order_ref": order_ref},
            ):
                pass
        except Exception:
            pass
