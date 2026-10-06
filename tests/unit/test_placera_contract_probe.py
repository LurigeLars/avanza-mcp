"""Temporary public Placera frontend contract probe. Remove before merge."""

from __future__ import annotations

import base64
import urllib.request


ASSETS = {
    "api": "https://forum.placera.se/assets/isObject-DvdnBDox.js",
    "auth": "https://forum.placera.se/assets/index.esm-C4jxjVQ6.js",
}


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "avanza-mcp-contract-probe/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def _window(source: str, marker: str, before: int = 250, after: int = 1200) -> str:
    index = source.find(marker)
    if index < 0:
        return "MISSING"
    return source[max(0, index-before):index+len(marker)+after]


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_public_placera_contract_probe():
    api = _fetch(ASSETS["api"])
    auth = _fetch(ASSETS["auth"])
    rows = {
        "set_token": _b64(_window(api, "setToken(e){", 100, 500)),
        "posts": _b64(_window(api, "posts:{", 50, 3500)),
        "bankid_sdk": _b64(_window(api, "bankid:{start", 100, 1000)),
        "auth_start_call": _b64(_window(auth, ".bankid.start(", 900, 3000)),
        "auth_collect_call": _b64(_window(auth, ".bankid.collect(", 900, 3500)),
        "auth_qr": _b64(_window(auth, "order_ref", 900, 3000)),
    }
    raise AssertionError("PLACERA_PROBE_B64=" + repr(rows))
