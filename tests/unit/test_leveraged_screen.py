from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp import Client

from avanza_mcp import mcp
from avanza_mcp.services.leveraged_screen_service import (
    LeveragedScreenService,
    ScreenFilters,
    _discovery_spread_percent,
    _timestamp_ms,
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
        self.market_data_quote_calls = []

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
                    totalValueTraded=123456,
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
                    leverage=5.1,
                    buyPrice=4.95,
                    sellPrice=5.05,
                    totalValueTraded=654321,
                )
            ],
            totalNumberOfOrderbooks=1,
        )

    async def get_authenticated_market_data_quote(self, order_book_id):
        self.market_data_quote_calls.append(order_book_id)
        if order_book_id == "101":
            return {
                "buy": 10.2,
                "sell": 10.3,
                "last": 10.25,
                "highest": 10.5,
                "lowest": 9.8,
                "change": 0.2,
                "changePercent": 2.0,
                "totalValueTraded": 1000,
                "totalVolumeTraded": 50,
                "timeOfLast": "2026-09-28T07:30:00.000+00:00",
                "updated": "2026-09-28T07:30:01.000+00:00",
            }
        return {
            "buy": 5.2,
            "sell": 5.3,
            "last": 5.25,
            "highest": 5.5,
            "lowest": 4.8,
            "change": 0.1,
            "changePercent": 1.9,
            "totalValueTraded": 2000,
            "totalVolumeTraded": 75,
            "timeOfLast": "2026-09-28T07:30:00.500+00:00",
            "updated": "2026-09-28T07:30:01.500+00:00",
        }


def test_trading_critical_naive_timestamp_uses_stockholm_timezone():
    assert _timestamp_ms("2026-09-28T10:38:19.837") == 1790584699837
    assert _timestamp_ms("2026-12-28T10:38:19.837") == 1798450699837


def test_discovery_spread_percent_is_midpoint_based_and_bounded():
    assert _discovery_spread_percent(9.9, 10.1) == 2.0
    assert _discovery_spread_percent(None, 10.1) is None
    assert _discovery_spread_percent(10.1, 9.9) is None
    assert _discovery_spread_percent(0, 10.1) is None


@pytest.mark.asyncio
async def test_snapshot_pagination_reuses_same_ranked_data_without_refetching():
    service = LeveragedScreenService(object())
    fake = FakeMarket()
    service._market = fake

    first = await service.screen("4478", "long", ["certificate", "warrant"], 1)
    assert first["snapshot"]["comparison_complete"] is True
    assert first["snapshot"]["scanned_count"] == 2
    assert first["snapshot"]["eligible_count"] == 2
    assert "complete_result_set" not in first["pagination"]
    assert first["pagination"] == {
        "total": 2,
        "offset": 0,
        "page_size": 1,
        "returned": 1,
        "has_more": True,
        "next_offset": 1,
    }

    calls = (len(fake.certificate_calls), len(fake.warrant_calls))
    second = service.get_page(first["snapshot_id"], "4478", "long", 1, 1)
    assert (len(fake.certificate_calls), len(fake.warrant_calls)) == calls
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["snapshot"] == first["snapshot"]
    assert second["pagination"]["has_more"] is False
    assert {first["products"][0]["order_book_id"], second["products"][0]["order_book_id"]} == {"101", "202"}


@pytest.mark.asyncio
async def test_realtime_enrichment_refetches_only_requested_snapshot_page():
    service = LeveragedScreenService(object())
    fake = FakeMarket()
    service._market = fake

    first = await service.screen("4478", "long", ["certificate", "warrant"], 2)
    enriched = await service.enrich_page(
        first["snapshot_id"],
        "4478",
        "long",
        0,
        2,
    )

    assert sorted(fake.market_data_quote_calls) == ["101", "202"]
    assert enriched["enrichment"]["attempted_count"] == 2
    assert enriched["enrichment"]["enriched_count"] == 2
    assert enriched["enrichment"]["authenticated_quote_count"] == 2
    assert enriched["enrichment"]["two_way_quote_count"] == 2
    assert enriched["structural_snapshot"]["ranking_quote_source"] == "delayed_filter_feed"
    assert enriched["ordering"] == "structural_snapshot_order"

    by_id = {product["order_book_id"]: product for product in enriched["products"]}
    assert by_id["101"]["discovery_bid"] == 9.9
    assert by_id["101"]["live_market_data"]["quote"]["bid"] == 10.2
    assert (
        by_id["101"]["live_market_data"]["quote"]["source"]
        == "authenticated_trading_critical"
    )
    assert (
        by_id["101"]["live_market_data"]["quote"]["freshness"]["source_updated_at"]
        == 1790580601000
    )
    assert by_id["202"]["live_market_data"]["quote"]["bid"] == 5.2


@pytest.mark.asyncio
async def test_realtime_enrichment_has_no_artificial_page_maximum():
    service = LeveragedScreenService(object())
    fake = FakeMarket()
    service._market = fake
    first = await service.screen("4478", "long", ["warrant"], 1)

    enriched = await service.enrich_page(
        first["snapshot_id"],
        "4478",
        "long",
        0,
        11,
    )

    assert enriched["pagination"]["page_size"] == 11
    assert enriched["pagination"]["returned"] == 1


@pytest.mark.asyncio
async def test_filters_apply_after_full_scan_and_before_ranking():
    service = LeveragedScreenService(object())
    fake = FakeMarket()
    service._market = fake
    filters = ScreenFilters(
        issuers=("issuer b",),
        sub_types=("turbo",),
        min_leverage=5.0,
        max_leverage=5.5,
        require_two_way_quote=True,
        max_spread_percent=2.1,
        min_turnover=600000,
    )

    result = await service.screen(
        "4478",
        "long",
        ["certificate", "warrant"],
        100,
        filters,
    )

    assert result["snapshot"]["scanned_count"] == 2
    assert result["snapshot"]["eligible_count"] == 1
    assert result["snapshot"]["eligible_quote_complete_count"] == 1
    assert result["pagination"]["total"] == 1
    assert result["products"][0]["order_book_id"] == "202"
    assert result["families"]["certificate"]["eligible_count"] == 0
    assert result["families"]["warrant"]["eligible_count"] == 1
    assert result["filters"] == {
        "issuers": ["issuer b"],
        "sub_types": ["turbo"],
        "min_leverage": 5.0,
        "max_leverage": 5.5,
        "require_two_way_quote": True,
        "max_spread_percent": 2.1,
        "min_turnover": 600000,
    }
    assert fake.certificate_calls[0].filter.issuers == ["issuer b"]
    assert fake.warrant_calls[0].filter.issuers == ["issuer b"]
    assert fake.warrant_calls[0].filter.subTypes == ["turbo"]


@pytest.mark.asyncio
async def test_threshold_filter_excludes_nonmatching_candidates():
    service = LeveragedScreenService(object())
    fake = FakeMarket()
    service._market = fake

    result = await service.screen(
        "4478",
        "long",
        ["certificate"],
        100,
        ScreenFilters(min_turnover=200000),
    )
    assert result["snapshot"]["scanned_count"] == 1
    assert result["pagination"]["total"] == 0


@pytest.mark.asyncio
async def test_invalid_filter_range_is_rejected():
    service = LeveragedScreenService(object())
    with pytest.raises(ValueError, match="min_leverage must be <= max_leverage"):
        await service.screen(
            "4478",
            "long",
            ["warrant"],
            100,
            ScreenFilters(min_leverage=6, max_leverage=5),
        )


class ConcurrentPaginatedWarrantMarket:
    def __init__(self):
        self.warrant_calls = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def filter_warrants(self, request):
        self.warrant_calls.append(request)
        if request.offset == 0:
            return SimpleNamespace(
                warrants=[
                    FakeItem(
                        orderbookId=str(index),
                        name=f"FIRST {index:03d}",
                        direction="long",
                        issuer="Issuer A",
                        buyPrice=10.0,
                        sellPrice=10.1,
                    )
                    for index in range(100)
                ],
                totalNumberOfOrderbooks=901,
            )

        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            remaining = min(100, 901 - request.offset)
            return SimpleNamespace(
                warrants=[
                    FakeItem(
                        orderbookId=str(request.offset + index),
                        name=f"PAGE {request.offset + index:03d}",
                        direction="long",
                        issuer="Issuer A",
                        buyPrice=10.0,
                        sellPrice=10.1,
                    )
                    for index in range(remaining)
                ],
                totalNumberOfOrderbooks=901,
            )
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_remaining_warrant_pages_use_bounded_concurrency_after_first_page():
    service = LeveragedScreenService(object())
    fake = ConcurrentPaginatedWarrantMarket()
    service._market = fake

    result = await service.screen("4478", "long", ["warrant"], 1)

    assert fake.warrant_calls[0].limit == 500
    assert {call.offset for call in fake.warrant_calls} == {
        0,
        100,
        200,
        300,
        400,
        500,
        600,
        700,
        800,
        900,
    }
    assert fake.max_in_flight == 8
    assert result["snapshot"]["scanned_count"] == 901
    assert result["families"]["warrant"]["scanned_count"] == 901
    assert result["pagination"]["total"] == 901


class CatalogCount:
    def __init__(self, count):
        self.count = count
        self.calls = []

    def count_by_underlying(self, underlying_order_book_id, *, direction=None, product_types=None):
        self.calls.append((underlying_order_book_id, direction, tuple(product_types or ())))
        return self.count


class LargePageWarrantMarket:
    def __init__(self):
        self.warrant_calls = []

    async def filter_warrants(self, request):
        self.warrant_calls.append(request)
        count = min(request.limit, max(0, 901 - request.offset))
        return SimpleNamespace(
            warrants=[
                FakeItem(
                    orderbookId=str(request.offset + index),
                    name=f"ROW {request.offset + index:04d}",
                    direction="long",
                    issuer="Issuer A",
                    buyPrice=10.0,
                    sellPrice=10.1,
                )
                for index in range(count)
            ],
            totalNumberOfOrderbooks=901,
        )


@pytest.mark.asyncio
async def test_large_upstream_page_reduces_request_count_when_supported():
    catalog = CatalogCount(901)
    service = LeveragedScreenService(object(), catalog=catalog)
    fake = LargePageWarrantMarket()
    service._market = fake

    result = await service.screen("4478", "long", ["warrant"], 1)

    assert [(call.offset, call.limit) for call in fake.warrant_calls] == [(0, 901)]
    assert catalog.calls == [("4478", "long", ("warrant",))]
    assert result["snapshot"]["scanned_count"] == 901
    assert result["pagination"]["total"] == 901


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
async def test_full_universe_is_ranked_before_page_size_is_applied():
    service = LeveragedScreenService(object())
    fake = PaginatedWarrantMarket()
    service._market = fake

    first = await service.screen("4478", "long", ["warrant"], 1)
    assert [call.offset for call in fake.warrant_calls] == [0, 100]
    assert first["families"]["warrant"]["scanned_count"] == 101
    assert first["pagination"]["total"] == 101
    assert first["pagination"]["has_more"] is True
    assert first["products"][0]["order_book_id"] == "999"

    before = len(fake.warrant_calls)
    remainder = service.get_page(first["snapshot_id"], "4478", "long", 1, 1000)
    assert len(fake.warrant_calls) == before
    assert remainder["pagination"]["returned"] == 100
    assert remainder["pagination"]["has_more"] is False


def test_snapshot_identity_mismatch_is_rejected():
    service = LeveragedScreenService(object())
    with pytest.raises(ValueError, match="not found or has expired"):
        service.get_page("0" * 32, "4478", "long", 0, 100)


@pytest.mark.asyncio
async def test_snapshot_filter_mismatch_is_rejected():
    service = LeveragedScreenService(object())
    fake = FakeMarket()
    service._market = fake
    first = await service.screen(
        "4478",
        "long",
        ["warrant"],
        1,
        ScreenFilters(issuers=("Issuer B",)),
    )

    with pytest.raises(ValueError, match="filters do not match"):
        service.get_page(
            first["snapshot_id"],
            "4478",
            "long",
            0,
            1,
            filters=ScreenFilters(issuers=("Issuer A",)),
        )


@pytest.mark.asyncio
async def test_screen_tool_has_unbounded_page_size_and_filter_contract():
    async with Client(mcp) as client:
        tools = {item.name: item for item in await client.list_tools()}
    tool = tools["screen_leveraged_instruments"]
    assert tool.output_schema is None
    props = tool.input_schema["properties"]
    assert props["page_size"]["minimum"] == 1
    assert "maximum" not in props["page_size"]
    assert props["offset"]["minimum"] == 0
    snapshot_schema = props["snapshot_id"]["anyOf"][0]
    assert snapshot_schema["pattern"] == "^[0-9a-f]{32}$"
    assert "max_per_type" not in props
    for field in (
        "issuers",
        "sub_types",
        "min_leverage",
        "max_leverage",
        "require_two_way_quote",
        "max_spread_percent",
        "min_turnover",
    ):
        assert field in props

    enrich = tools["enrich_leveraged_snapshot"]
    enrich_props = enrich.input_schema["properties"]
    assert enrich_props["page_size"]["minimum"] == 1
    assert "maximum" not in enrich_props["page_size"]
    assert enrich_props["page_size"]["default"] == 5
    assert enrich_props["snapshot_id"]["pattern"] == "^[0-9a-f]{32}$"


@pytest.mark.asyncio
async def test_public_filter_tools_remain_capped_at_100_rows():
    async with Client(mcp) as client:
        tools = {item.name: item for item in await client.list_tools()}

    assert tools["filter_certificates"].input_schema["properties"]["limit"]["maximum"] == 100
    assert tools["filter_warrants"].input_schema["properties"]["limit"]["maximum"] == 100


def test_internal_leveraged_request_has_no_artificial_maximum():
    from avanza_mcp.services.leveraged_screen_service import (
        _LeveragedCertificateFilterRequest,
        _LeveragedWarrantFilterRequest,
    )

    assert "maximum" not in _LeveragedCertificateFilterRequest.model_json_schema()["properties"]["limit"]
    assert "maximum" not in _LeveragedWarrantFilterRequest.model_json_schema()["properties"]["limit"]


class FilterOptionsWarrantMarket:
    def __init__(self):
        self.warrant_calls = []

    async def filter_warrants(self, request):
        self.warrant_calls.append(request)
        return SimpleNamespace(
            warrants=[
                FakeItem(
                    orderbookId="202",
                    name="MINI L TEST",
                    direction="long",
                    issuer="Morgan Stanley",
                    subType="MINI_FUTURE",
                    leverage=4.0,
                    buyPrice=10.0,
                    sellPrice=10.1,
                )
            ],
            totalNumberOfOrderbooks=1,
            model_dump=lambda **_kwargs: {
                "filterOptions": {
                    "issuers": [
                        {
                            "value": "morgan stanley",
                            "displayName": "Morgan Stanley",
                            "numberOfOrderbooks": 1,
                        },
                        {
                            "value": "vontobel",
                            "displayName": "Vontobel",
                            "numberOfOrderbooks": 2,
                        },
                        {
                            "value": "unused",
                            "displayName": "Unused",
                            "numberOfOrderbooks": 0,
                        },
                    ],
                    "subTypes": [
                        {
                            "value": "mini_future",
                            "displayName": "Mini Future",
                            "numberOfOrderbooks": 1,
                        },
                        {
                            "value": "knock_out",
                            "displayName": "Unlimited Turbo",
                            "numberOfOrderbooks": 2,
                        },
                    ],
                }
            },
        )


@pytest.mark.asyncio
async def test_filter_options_preserve_structural_discovery_values_after_pushdown():
    service = LeveragedScreenService(object())
    fake = FilterOptionsWarrantMarket()
    service._market = fake

    result = await service.screen(
        "4478",
        "long",
        ["warrant"],
        100,
        ScreenFilters(
            issuers=("MORGAN STANLEY",),
            sub_types=("MINI_FUTURE",),
        ),
    )

    request_filter = fake.warrant_calls[0].filter
    assert request_filter.issuers == ["morgan stanley"]
    assert request_filter.subTypes == ["mini_future"]
    assert result["available_filter_values"]["issuers"] == [
        "Morgan Stanley",
        "Vontobel",
    ]
    assert result["available_filter_values"]["sub_types"] == [
        "Mini Future",
        "Unlimited Turbo",
    ]
    assert result["pagination"]["total"] == 1
