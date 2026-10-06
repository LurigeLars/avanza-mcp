"""Temporary public Placera frontend contract probe. Remove before merge."""

from __future__ import annotations

import html
import urllib.request

import pytest

ASSETS = {
    "api": "https://forum.placera.se/assets/isObject-DvdnBDox.js",
    "auth": "https://forum.placera.se/assets/index.esm-C4jxjVQ6.js",
}


def _fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "avanza-mcp-contract-probe/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def _snippets(source: str, terms: list[str], radius: int = 900) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for term in terms:
        rows = []
        start = 0
        while len(rows) < 4:
            index = source.find(term, start)
            if index < 0:
                break
            rows.append(source[max(0, index - radius): index + len(term) + radius])
            start = index + len(term)
        result[term] = rows
    return result


@pytest.mark.parametrize("asset", ["api", "auth"])
def test_public_placera_contract_probe(asset):
    source = html.unescape(_fetch(ASSETS[asset]))
    terms = (
        [
            "Authorization",
            "Bearer",
            "/v1/posts",
            "posts",
            "STORE_TOKEN",
            "setToken",
            "api.forum.placera.se",
        ]
        if asset == "api"
        else [
            "bankid",
            "order_ref",
            "collect",
            "qr",
            "token",
            "/v1/auth/",
        ]
    )
    found = _snippets(source, terms)
    raise AssertionError(f"PLACERA_PROBE_{asset.upper()}={found!r}")
