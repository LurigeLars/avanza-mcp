from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp import Client

from avanza_mcp import mcp
from avanza_mcp.services.leveraged_screen_service import (
    LeveragedScreenService,
    _discovery_spread_percent,
)


class FakeItem:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **_kwargs):
        return dict(self.data)


class FakeMarket:
    def __init__(self):
        self.certificate_calls = []
        self.warrant_calls = []

    async def filter_certificates(self, request):
        self.certificate_calls.append(request)
        return SimpleNamespace(
            certificates=[
                FakeItem(
                    orderbookId="101",
                    name="MINI L TEST",
                    direction="long",
                    issuer="Issuer A",
                    leverage=4.2,
                    buyPrice=9.9,
                    sellPrice=10.1,
                    spread=0.02,
                    totalValueTraded=123456,
                    underlyingInstrument={
                        "orderbookId": "4478",
                        "name": "NVIDIA",
                        "instrumentType": "STOCK",
                    },
                )
            ],
            totalNumberOfOrderbooks=1,
        )

    async def filter_warrants(self, request):
        self.warrant_calls.append(request)
        return SimpleNamespace(
            warrants=[
                FakeItem(
                    orderbookId="202",
                    name="TURBO L TEST",
                    direction="long",
                    issuer="Issuer B",
                    subType="TURBO",
                    stopLoss=8.0,
                    buyPrice=4.95,
                    sellPrice=5.05,
                    totalValueTraded=654321,
                    underlyingInstrument={
                        "orderbookId": "4478",
                        "name": "NVIDIA",
                        "instrumentType": "STOCK",
                    },
                )
            ],
            totalNumberOfOrderbooks=1,
        )


def test_discovery_spread_percent_is_midpoint_based_and_bounded():
    assert _discovery_spread_percent(9.9, 10.1) == 2.0
    assert _discovery_spread_percent(None, 10.1) is None
    assert _discovery_spread_percent(10.1, 9.9) is None
    assert _discovery_spread_percent(0, 10.1) is None


@pytest.mark.asyncio
async def test_screen_aggregates_certificate_and_warrant_families():
    service = LeveragedScreenService(object())
    fake = FakeMarket()
    service._market = fake

    result = await service.screen(
        "4478", "long", ["certificate", "warrant"], 100
    )

    assert result["returned"] == 2
    assert result["families"]["certificate"] == {
        "returned": 1,
        "upstream_total": 1,
        "scanned": 1,
        "truncated": False,
    }
    assert result["families"]["warrant"] == {
        "returned": 1,
        "upstream_total": 1,
        "scanned": 1,
        "truncated": False,
    }
    assert result["products"][0]["spread_percent_from_discovery_prices"] == 2.0
    assert result["products"][1]["sub_type"] == "TURBO"
    assert fake.certificate_calls[0].filter.underlyingInstruments == ["4478"]
    assert fake.warrant_calls[0].filter.underlyingInstruments == ["4478"]
    assert fake.certificate_calls[0].filter.directions == ["long"]
    assert fake.warrant_calls[0].filter.directions == ["long"]


class PaginatedWarrantMarket:
    def __init__(self):
        self.warrant_calls = []

    async def filter_warrants(self, request):
        self.warrant_calls.append(request)
        if request.offset == 0:
            return SimpleNamespace(
                warrants=[
                    FakeItem(
                        orderbookId=str(index),
                        name=f"EARLY {index:03d}",
                        direction="long",
                        issuer="Issuer A",
                        buyPrice=10.0,
                        sellPrice=10.5,
                        totalValueTraded=0,
                    )
                    for index in range(100)
                ],
                totalNumberOfOrderbooks=101,
            )
        return SimpleNamespace(
            warrants=[
                FakeItem(
                    orderbookId="999",
                    name="LATE BEST",
                    direction="long",
                    issuer="Issuer B",
                    buyPrice=10.0,
                    sellPrice=10.01,
                    totalValueTraded=1_000_000,
                )
            ],
            totalNumberOfOrderbooks=101,
        )


@pytest.mark.asyncio
async def test_screen_pages_full_matching_universe_before_ranking_and_limit():
    service = LeveragedScreenService(object())
    fake = PaginatedWarrantMarket()
    service._market = fake

    result = await service.screen("4478", "long", ["warrant"], 1)

    assert [call.offset for call in fake.warrant_calls] == [0, 100]
    assert result["families"]["warrant"] == {
        "returned": 1,
        "upstream_total": 101,
        "scanned": 101,
        "truncated": True,
    }
    assert result["products"][0]["order_book_id"] == "999"
    assert result["ranking"] == "two_way_quote, spread_percent_asc, turnover_desc"


@pytest.mark.asyncio
async def test_screen_tool_is_unstructured_and_has_no_output_schema():
    async with Client(mcp) as client:
        listed = await client.list_tools()
    tool = next(item for item in listed if item.name == "screen_leveraged_instruments")
    assert tool.output_schema is None
