from __future__ import annotations

import json

import pytest
from fastmcp import Client

from avanza_mcp import mcp
from avanza_mcp.models.warrant import WarrantFilterResponse
from avanza_mcp.tools._helpers import shape_filter_options


def _response() -> WarrantFilterResponse:
    return WarrantFilterResponse.model_validate(
        {
            "totalNumberOfOrderbooks": 1,
            "warrants": [],
            "filterOptions": {
                "issuers": [
                    {
                        "value": "issuer-a",
                        "displayName": "Issuer A",
                        "numberOfOrderbooks": 1,
                    }
                ],
                "underlyingInstruments": [
                    {
                        "value": str(index),
                        "displayName": f"Underlying {index}",
                        "numberOfOrderbooks": 1,
                    }
                    for index in range(1000)
                ],
                "categories": [
                    {
                        "value": "equity",
                        "displayName": "Equity",
                        "numberOfOrderbooks": 1,
                        "children": [
                            {
                                "value": "single-stock",
                                "displayName": "Single stock",
                                "numberOfOrderbooks": 1,
                                "children": [
                                    {
                                        "value": "nested",
                                        "displayName": "Nested",
                                        "numberOfOrderbooks": 1,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "directions": [
                    {
                        "value": "long",
                        "displayName": "Long",
                        "numberOfOrderbooks": 1,
                    }
                ],
            },
        }
    )


def test_compact_filter_options_removes_heavy_metadata_but_keeps_small_vocabularies():
    raw = _response()
    compact = shape_filter_options(raw, "compact")
    payload = compact.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert "underlyingInstruments" not in payload["filterOptions"]
    assert payload["filterOptions"]["issuers"][0]["value"] == "issuer-a"
    assert payload["filterOptions"]["directions"][0]["value"] == "long"
    assert payload["filterOptions"]["categories"] == [
        {
            "value": "equity",
            "displayName": "Equity",
            "numberOfOrderbooks": 1,
        }
    ]
    assert payload["filterOptionsSummary"]["underlying_instruments_omitted"] == 1000
    assert payload["filterOptionsSummary"]["category_descendants_omitted"] == 2

    raw_size = len(json.dumps(raw.model_dump(mode="json", by_alias=True, exclude_none=True)))
    compact_size = len(json.dumps(payload))
    assert compact_size < raw_size * 0.1


def test_full_filter_options_preserves_upstream_response():
    raw = _response()
    full = shape_filter_options(raw, "full")
    payload = full.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert len(payload["filterOptions"]["underlyingInstruments"]) == 1000
    assert "children" in payload["filterOptions"]["categories"][0]
    assert "filterOptionsSummary" not in payload


def test_none_filter_options_removes_metadata_and_reports_intentional_omission():
    compact = shape_filter_options(_response(), "none")
    payload = compact.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert "filterOptions" not in payload
    assert payload["filterOptionsSummary"]["mode"] == "none"
    assert payload["filterOptionsSummary"]["underlying_instruments_omitted"] == 1000


@pytest.mark.asyncio
async def test_leveraged_raw_filter_tools_default_to_compact_filter_options():
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("filter_certificates", "filter_warrants"):
        props = tools[name].input_schema["properties"]
        assert props["filter_options_mode"]["enum"] == ["compact", "full", "none"]
        assert props["filter_options_mode"]["default"] == "compact"
