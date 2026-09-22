from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp import Client

from avanza_mcp import mcp
from avanza_mcp.services.options_screen_service import (
    OptionScreenSpec,
    OptionsScreenService,
)


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def model_dump(self, **_kwargs):
        return self.data


class FakeMarket:
    def __init__(self):
        self.calls = []

    async def list_futures_forwards(self, request):
        self.calls.append(request)
        option_type = request.filter.optionTypes[0] if request.filter.optionTypes else None
        expiry = request.filter.endDates[0] if request.filter.endDates else None

        filter_options = {
            "optionTypes": [
                {"value": "STANDARD", "numberOfOrderbooks": 4},
                {"value": "WEEKLY", "numberOfOrderbooks": 0},
            ],
            "callIndicators": [
                {"value": "CALL", "numberOfOrderbooks": 2},
                {"value": "PUT", "numberOfOrderbooks": 2},
            ],
            "endDates": [
                {
                    "value": "2026-10",
                    "children": [
                        {"value": "2026-10-16", "numberOfOrderbooks": 4},
                    ],
                }
            ],
        }
        if expiry:
            rows = [
                {
                    "call": {
                        "orderbookId": "101",
                        "name": "TEST CALL 100",
                        "countryCode": "SE",
                        "strikePrice": 100,
                        "callIndicator": "Köpoption",
                    },
                    "put": {
                        "orderbookId": "102",
                        "name": "TEST PUT 100",
                        "countryCode": "SE",
                        "strikePrice": 100,
                        "callIndicator": "Säljoption",
                    },
                },
                {
                    "call": {
                        "orderbookId": "103",
                        "name": "TEST CALL 110",
                        "countryCode": "SE",
                        "strikePrice": 110,
                        "callIndicator": "Köpoption",
                    },
                    "put": {
                        "orderbookId": "104",
                        "name": "TEST PUT 110",
                        "countryCode": "SE",
                        "strikePrice": 110,
                        "callIndicator": "Säljoption",
                    },
                },
            ]
            return FakeResponse(
                {
                    "matchedOptions": rows,
                    "totalNumberOfOrderbooks": 2,
                    "filterOptions": filter_options,
                    "underlyingInstrument": {"orderbookId": "5269", "name": "Test B"},
                }
            )
        return FakeResponse(
            {
                "matchedOptions": [],
                "totalNumberOfOrderbooks": 0,
                "filterOptions": filter_options,
                "underlyingInstrument": {"orderbookId": "5269", "name": "Test B"},
            }
        )


@pytest.mark.asyncio
async def test_options_screen_discovers_expiry_flattens_pairs_and_filters():
    service = OptionsScreenService(object())
    fake = FakeMarket()
    service._market = fake

    result = await service.screen(
        "5269",
        100,
        OptionScreenSpec(
            option_types=("STANDARD",),
            call_indicators=("CALL",),
            min_strike=105,
        ),
    )

    assert result["snapshot"]["comparison_complete"] is True
    assert result["snapshot"]["market_data_enriched"] is False
    assert result["snapshot"]["scanned_pair_rows"] == 2
    assert result["snapshot"]["scanned_contract_count"] == 4
    assert result["snapshot"]["eligible_count"] == 1
    assert result["pagination"]["total"] == 1
    assert result["options"][0]["order_book_id"] == "103"
    assert result["options"][0]["expiry_date"] == "2026-10-16"
    assert result["options"][0]["option_type"] == "STANDARD"
    assert result["available_filter_values"]["option_types"] == ["STANDARD"]
    assert result["available_filter_values"]["call_indicators"] == ["CALL", "PUT"]
    assert result["available_filter_values"]["end_dates"] == ["2026-10-16"]
    assert result["available_filter_values"]["strike"] == {
        "reported_count": 4,
        "min": 100.0,
        "max": 110.0,
    }


@pytest.mark.asyncio
async def test_options_snapshot_pages_without_refetching():
    service = OptionsScreenService(object())
    fake = FakeMarket()
    service._market = fake
    first = await service.screen(
        "5269",
        1,
        OptionScreenSpec(option_types=("STANDARD",)),
    )
    calls = len(fake.calls)

    second = service.get_page(first["snapshot_id"], "5269", 1, 2)
    assert len(fake.calls) == calls
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["pagination"]["offset"] == 1


@pytest.mark.asyncio
async def test_screen_options_contract_is_unstructured_and_unbounded_page_size():
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    tool = tools["screen_options"]
    assert tool.output_schema is None
    props = tool.input_schema["properties"]
    assert props["page_size"]["minimum"] == 1
    assert "maximum" not in props["page_size"]
    assert props["call_indicators"]["anyOf"][0]["items"]["enum"] == ["CALL", "PUT"]
    assert props["end_dates"]["anyOf"][0]["items"]["format"] == "date"
