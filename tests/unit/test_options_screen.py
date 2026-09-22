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
        self.option_info_calls = []

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

    async def get_option_info(self, order_book_id):
        self.option_info_calls.append(order_book_id)
        return FakeResponse(
            {
                "orderbookId": order_book_id,
                "name": f"OPTION {order_book_id}",
                "isin": f"SE{order_book_id}",
                "tradable": "BUYABLE_AND_SELLABLE",
                "type": "OPTION",
                "keyIndicators": {
                    "callIndicator": "Köp",
                    "endDate": "2026-10-16",
                    "strikePrice": 100 if order_book_id in {"101", "102"} else 110,
                    "subType": "STANDARD",
                },
                "quote": {
                    "buy": 10.0,
                    "sell": 10.5,
                    "last": 10.2,
                    "spread": 4.88,
                    "totalValueTraded": 12345,
                    "totalVolumeTraded": 100,
                    "updated": 123456789,
                    "isRealTime": False,
                },
                "underlying": {
                    "orderbookId": "5269",
                    "name": "Test B",
                    "quote": {
                        "buy": 99.0,
                        "sell": 99.1,
                        "last": 99.05,
                        "updated": 123456700,
                        "isRealTime": False,
                    },
                },
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
async def test_option_enrichment_pages_existing_snapshot_without_matrix_refetch():
    service = OptionsScreenService(object())
    fake = FakeMarket()
    service._market = fake
    first = await service.screen(
        "5269",
        1,
        OptionScreenSpec(option_types=("STANDARD",)),
    )
    matrix_calls = len(fake.calls)

    enriched = await service.enrich_page(
        first["snapshot_id"],
        "5269",
        1,
        2,
    )

    assert len(fake.calls) == matrix_calls
    assert fake.option_info_calls == ["102", "103"]
    assert enriched["pagination"] == {
        "total": 4,
        "offset": 1,
        "page_size": 2,
        "returned": 2,
        "has_more": True,
        "next_offset": 3,
    }
    assert enriched["enrichment"]["attempted_count"] == 2
    assert enriched["enrichment"]["enriched_count"] == 2
    assert enriched["enrichment"]["not_found_count"] == 0
    assert enriched["enrichment"]["atomic"] is False
    assert enriched["options"][0]["market_data"]["instrument_type"] == "OPTION"
    assert enriched["options"][0]["market_data"]["quote"] == {
        "bid": 10.0,
        "ask": 10.5,
        "last": 10.2,
        "upstream_spread_percent": 4.88,
        "total_value_traded": 12345,
        "total_volume_traded": 100,
        "updated": 123456789,
        "is_real_time": False,
    }
    assert enriched["options"][0]["market_data"]["underlying_quote"]["is_real_time"] is False


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

    enrich = tools["enrich_option_snapshot"]
    assert enrich.output_schema is None
    enrich_props = enrich.input_schema["properties"]
    assert enrich_props["page_size"]["minimum"] == 1
    assert "maximum" not in enrich_props["page_size"]
    assert enrich_props["page_size"]["default"] == 20
    assert enrich_props["snapshot_id"]["pattern"] == "^[0-9a-f]{32}$"
