from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastmcp import Client

from avanza_mcp import mcp
from avanza_mcp.services.leveraged_screen_service import (
    LeveragedScreenService,
    ScreenFilters,
    SuitabilityCriteria,
    _discovery_spread_percent,
    _execution_rank,
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
        self.market_data_batch_calls = []

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
                    stopLoss=4.0,
                    buyPrice=4.95,
                    sellPrice=5.05,
                    totalValueTraded=654321,
                )
            ],
            totalNumberOfOrderbooks=1,
        )

    async def get_authenticated_market_data_quotes(self, order_book_ids):
        self.market_data_batch_calls.append(list(order_book_ids))
        return [
            await self.get_authenticated_market_data_quote(order_book_id)
            for order_book_id in order_book_ids
        ]

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


def test_execution_rank_uses_bid_ask_freshness_not_last_trade_age():
    fresh_quote_old_trade = {
        "order_book_id": "fresh",
        "live_market_data": {
            "quote": {
                "bid": 10.0,
                "ask": 10.01,
                "spread_percent_from_live_prices": 0.09995,
                "freshness": {
                    "bid_ask_age_ms": 2_000,
                    "last_trade_age_ms": 86_400_000,
                },
            }
        },
    }
    stale_quote_recent_trade = {
        "order_book_id": "stale",
        "live_market_data": {
            "quote": {
                "bid": 10.0,
                "ask": 10.001,
                "spread_percent_from_live_prices": 0.0099995,
                "freshness": {
                    "bid_ask_age_ms": 120_000,
                    "last_trade_age_ms": 500,
                },
            }
        },
    }

    ranked = sorted(
        [stale_quote_recent_trade, fresh_quote_old_trade],
        key=_execution_rank,
    )
    assert [item["order_book_id"] for item in ranked] == ["fresh", "stale"]



def test_execution_rank_prefers_target_leverage_before_spread_when_requested():
    exact_target_wider_spread = {
        "order_book_id": "exact",
        "suitability": {"leverage_deviation": 0.0},
        "live_market_data": {
            "quote": {
                "bid": 10.0,
                "ask": 10.02,
                "spread_percent_from_live_prices": 0.1998,
                "freshness": {"bid_ask_age_ms": 2_000},
            }
        },
    }
    off_target_tighter_spread = {
        "order_book_id": "off-target",
        "suitability": {"leverage_deviation": 0.8},
        "live_market_data": {
            "quote": {
                "bid": 10.0,
                "ask": 10.001,
                "spread_percent_from_live_prices": 0.0099995,
                "freshness": {"bid_ask_age_ms": 1_000},
            }
        },
    }

    default_ranked = sorted(
        [exact_target_wider_spread, off_target_tighter_spread],
        key=_execution_rank,
    )
    assert [item["order_book_id"] for item in default_ranked] == [
        "off-target",
        "exact",
    ]

    target_ranked = sorted(
        [exact_target_wider_spread, off_target_tighter_spread],
        key=lambda item: _execution_rank(item, prefer_target_leverage=True),
    )
    assert [item["order_book_id"] for item in target_ranked] == [
        "exact",
        "off-target",
    ]


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

    expected_order = [product["order_book_id"] for product in first["products"]]
    assert fake.market_data_batch_calls == [expected_order]
    assert fake.market_data_quote_calls == expected_order
    assert enriched["enrichment"]["attempted_count"] == 2
    assert enriched["enrichment"]["enriched_count"] == 2
    assert enriched["enrichment"]["authenticated_quote_count"] == 2
    assert enriched["enrichment"]["auth_worker_calls"] == 1
    assert (
        enriched["enrichment"]["source"]
        == "authenticated_trading_critical_market_data_batch"
    )
    assert enriched["enrichment"]["two_way_quote_count"] == 2
    assert enriched["structural_snapshot"]["ranking_quote_source"] == "delayed_filter_feed"
    assert enriched["ordering"] == "execution_ranking"
    assert enriched["execution_freshness_basis"] == "bid_ask_updated_at"
    assert enriched["last_trade_role"] == "informational_only_for_leveraged_products"
    assert enriched["execution_ranking"].startswith("fresh_two_way_quote")

    by_id = {product["order_book_id"]: product for product in enriched["products"]}
    assert by_id["101"]["discovery_bid"] == 9.9
    assert by_id["101"]["live_market_data"]["quote"]["bid"] == 10.2
    assert (
        by_id["101"]["live_market_data"]["quote"]["source"]
        == "authenticated_trading_critical"
    )
    freshness = by_id["101"]["live_market_data"]["quote"]["freshness"]
    assert freshness["source_updated_at"] == 1790580601000
    assert freshness["execution_freshness_basis"] == "bid_ask_updated_at"
    assert freshness["execution_stale_after_ms"] == 30_000
    assert freshness["last_trade_role"] == "informational_only_for_leveraged_products"
    assert by_id["202"]["live_market_data"]["quote"]["bid"] == 5.2



class LocalUniverseCatalog:
    def __init__(self):
        self.rows = [
            {
                "product_type": "warrant",
                "order_book_id": "101",
                "name": "MINI L TEST",
                "direction": "long",
                "issuer": "Issuer A",
                "sub_type": "MINI_FUTURE",
                "leverage": 4.2,
                "stop_loss": 8.0,
                "underlying_order_book_id": "4478",
                "underlying_name": "NVIDIA",
                "underlying_instrument_type": "STOCK",
                "underlying_country_code": "US",
            },
            {
                "product_type": "warrant",
                "order_book_id": "202",
                "name": "TURBO L TEST",
                "direction": "long",
                "issuer": "Issuer B",
                "sub_type": "KNOCK_OUT",
                "leverage": 5.1,
                "stop_loss": 4.0,
                "underlying_order_book_id": "4478",
                "underlying_name": "NVIDIA",
            },
        ]

    def count_by_underlying(self, *_args, **_kwargs):
        return len(self.rows)

    def find_by_underlying(self, *_args, limit=1000, **_kwargs):
        return list(self.rows[:limit])



@pytest.mark.asyncio
async def test_leverage_filter_falls_back_from_catalog_to_filter_feed():
    service = LeveragedScreenService(object(), catalog=LocalUniverseCatalog())
    fake = FakeMarket()
    service._market = fake

    result = await service.screen(
        "4478",
        "long",
        ["warrant"],
        10,
        filters=ScreenFilters(min_leverage=4.0, max_leverage=6.0),
    )

    assert result["snapshot"]["discovery_source"] == "avanza_filter_feed"
    assert len(fake.warrant_calls) == 1
    assert result["pagination"]["total"] == 1
    assert result["products"][0]["leverage"] == pytest.approx(5.1)


@pytest.mark.asyncio
async def test_target_leverage_suitability_falls_back_from_catalog_to_filter_feed():
    service = LeveragedScreenService(object(), catalog=LocalUniverseCatalog())
    fake = FakeMarket()
    service._market = fake

    result = await service.screen(
        "4478",
        "long",
        ["warrant"],
        10,
        suitability=SuitabilityCriteria(
            target_leverage=5.0,
            max_leverage_deviation=0.2,
        ),
    )

    assert result["snapshot"]["discovery_source"] == "avanza_filter_feed"
    assert len(fake.warrant_calls) == 1
    assert result["pagination"]["total"] == 1
    assert result["products"][0]["suitability"]["leverage_deviation"] == pytest.approx(0.1)



@pytest.mark.asyncio
async def test_target_leverage_controls_execution_order_before_spread():
    service = LeveragedScreenService(object(), catalog=LocalUniverseCatalog())
    fake = FakeMarket()
    service._market = fake

    first = await service.screen(
        "4478",
        "long",
        ["certificate", "warrant"],
        10,
        suitability=SuitabilityCriteria(
            target_leverage=5.0,
            max_leverage_deviation=1.0,
        ),
    )
    enriched = await service.enrich_snapshot(
        first["snapshot_id"], "4478", "long", 0, 10
    )

    assert enriched["execution_ranking"].startswith(
        "fresh_two_way_quote, leverage_deviation_asc"
    )
    assert [item["order_book_id"] for item in enriched["products"][:2]] == [
        "202",
        "101",
    ]


@pytest.mark.asyncio
async def test_catalog_is_primary_universe_and_global_enrichment_precedes_paging():
    service = LeveragedScreenService(object(), catalog=LocalUniverseCatalog())
    fake = FakeMarket()
    service._market = fake

    first = await service.screen("4478", "long", ["warrant"], 1)
    assert first["snapshot"]["discovery_source"] == "fresh_local_instrument_catalog"
    assert first["snapshot"]["scanned_count"] == 2
    assert fake.warrant_calls == []

    enriched = await service.enrich_snapshot(
        first["snapshot_id"], "4478", "long", 0, 1
    )
    assert len(fake.market_data_batch_calls) == 2
    assert fake.market_data_batch_calls[0] == ["101", "202"]
    assert set(fake.market_data_batch_calls[1]) == {"101", "202"}
    assert enriched["enrichment"]["attempted_count"] == 2
    assert enriched["enrichment"]["scope"] == "complete_snapshot_progressive"
    assert enriched["pagination"]["total"] == 2
    assert enriched["pagination"]["returned"] == 1

    second = await service.enrich_snapshot(
        first["snapshot_id"], "4478", "long", 1, 1
    )
    assert len(fake.market_data_batch_calls) == 2
    assert second["enrichment"]["cache_hit"] is True
    assert second["pagination"]["returned"] == 1





@pytest.mark.asyncio
async def test_suitability_filters_target_leverage_and_stop_loss_buffer():
    service = LeveragedScreenService(object(), catalog=LocalUniverseCatalog())
    fake = FakeMarket()
    service._market = fake

    result = await service.screen(
        "4478",
        "long",
        ["warrant"],
        10,
        suitability=SuitabilityCriteria(
            target_leverage=5.0,
            max_leverage_deviation=0.2,
            min_stop_loss_buffer_percent=10.0,
        ),
    )

    assert result["pagination"]["total"] == 1
    assert result["products"][0]["order_book_id"] == "202"
    assert result["products"][0]["suitability"]["leverage_deviation"] == pytest.approx(0.1)
    assert result["products"][0]["suitability"]["stop_loss_buffer_percent"] > 20
    assert result["suitability"] == {
        "target_leverage": 5.0,
        "max_leverage_deviation": 0.2,
        "min_stop_loss_buffer_percent": 10.0,
    }
    assert result["suitability_context"]["underlying_reference_source"] == (
        "authenticated_trading_critical"
    )
    assert fake.market_data_quote_calls == ["4478"]


def test_suitability_requires_explicit_leverage_tolerance():
    service = LeveragedScreenService(object(), catalog=LocalUniverseCatalog())

    with pytest.raises(
        ValueError,
        match="target_leverage and max_leverage_deviation must be supplied together",
    ):
        asyncio.run(
            service.screen(
                "4478",
                "long",
                ["warrant"],
                10,
                suitability=SuitabilityCriteria(target_leverage=5.0),
            )
        )


class FourRowLocalUniverseCatalog(LocalUniverseCatalog):
    def __init__(self):
        super().__init__()
        self.rows.extend(
            [
                {
                    **self.rows[0],
                    "order_book_id": "303",
                    "name": "MINI L TEST 303",
                },
                {
                    **self.rows[1],
                    "order_book_id": "404",
                    "name": "TURBO L TEST 404",
                },
            ]
        )


@pytest.mark.asyncio
async def test_full_execution_scan_progresses_by_internal_batch_and_ranks_only_when_complete(
    monkeypatch,
):
    monkeypatch.setattr(
        "avanza_mcp.services.leveraged_screen_service._EXECUTION_BATCH_SIZE", 2
    )
    service = LeveragedScreenService(object(), catalog=FourRowLocalUniverseCatalog())
    fake = FakeMarket()
    service._market = fake

    first = await service.screen("4478", "long", ["warrant"], 1)

    partial = await service.enrich_snapshot(
        first["snapshot_id"], "4478", "long", 0, 1
    )
    assert len(fake.market_data_batch_calls) == 1
    assert len(fake.market_data_batch_calls[0]) == 2
    assert partial["enrichment"]["attempted_count"] == 2
    assert partial["enrichment"]["remaining_count"] == 2
    assert partial["enrichment"]["scan_complete"] is False
    assert partial["ordering"] == "pending_global_execution_ranking"
    assert partial["products"] == []
    assert partial["returned"] == 0

    complete = await service.enrich_snapshot(
        first["snapshot_id"], "4478", "long", 0, 1
    )
    assert len(fake.market_data_batch_calls) == 3
    assert {
        order_book_id
        for batch in fake.market_data_batch_calls[:2]
        for order_book_id in batch
    } == {"101", "202", "303", "404"}
    assert set(fake.market_data_batch_calls[2]) == {"101", "202", "303", "404"}
    assert complete["enrichment"]["attempted_count"] == 4
    assert complete["enrichment"]["remaining_count"] == 0
    assert complete["enrichment"]["scan_complete"] is True
    assert complete["ordering"] == "execution_ranking"
    assert complete["pagination"]["total"] == 4
    assert complete["pagination"]["returned"] == 1
    assert complete["enrichment"]["final_refresh"]["requested_count"] == 4
    assert complete["enrichment"]["final_refresh"]["refreshed_count"] == 4

    cached = await service.enrich_snapshot(
        first["snapshot_id"], "4478", "long", 1, 1
    )
    assert len(fake.market_data_batch_calls) == 3
    assert cached["enrichment"]["cache_hit"] is True
    assert cached["pagination"]["returned"] == 1


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
async def test_screen_tool_returns_public_fallback_when_auth_is_unavailable(monkeypatch):
    async def fake_screen(self, *_args, **_kwargs):
        return {
            "snapshot_id": "a" * 32,
            "underlying_order_book_id": "4478",
            "direction": "long",
            "product_types": ["warrant"],
            "filters": {},
            "families": {},
            "snapshot": {},
            "pagination": {
                "total": 1,
                "offset": 0,
                "page_size": 1,
                "returned": 1,
                "has_more": False,
                "next_offset": None,
            },
            "products": [
                {
                    "product_type": "warrant",
                    "order_book_id": "202",
                    "name": "MINI L TEST",
                    "discovery_bid": 10.0,
                    "discovery_ask": 10.1,
                }
            ],
            "returned": 1,
            "ranking": "two_way_quote, spread_percent_asc, turnover_desc",
        }

    async def fake_enrich(self, *_args, **_kwargs):
        return {
            "products": [
                {
                    "order_book_id": "202",
                    "live_market_data_error": "auth_required",
                }
            ],
            "returned": 1,
            "enrichment": {
                "authenticated_quote_count": 0,
                "scan_complete": False,
                "error": "auth_required",
            },
        }

    monkeypatch.setattr(LeveragedScreenService, "screen", fake_screen)
    monkeypatch.setattr(LeveragedScreenService, "enrich_snapshot", fake_enrich)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "screen_leveraged_instruments",
            {
                "underlying_order_book_id": "4478",
                "direction": "long",
                "product_types": ["warrant"],
                "page_size": 1,
            },
        )

    payload = json.loads(result.content[0].text)
    assert payload["products"][0]["discovery_bid"] == 10.0
    assert payload["execution"] == {
        "status": "public_fallback",
        "reason": "auth_required",
        "data_quality": "discovery_only",
        "enrichment": {
            "authenticated_quote_count": 0,
            "scan_complete": False,
            "error": "auth_required",
        },
    }



@pytest.mark.asyncio
async def test_screen_tool_hides_provisional_ranking_while_full_scan_is_partial(monkeypatch):
    async def fake_screen(self, *_args, **_kwargs):
        return {
            "snapshot_id": "b" * 32,
            "underlying_order_book_id": "4478",
            "direction": "long",
            "product_types": ["warrant"],
            "filters": {},
            "families": {},
            "snapshot": {"discovery_source": "fresh_local_instrument_catalog"},
            "pagination": {
                "total": 300,
                "offset": 0,
                "page_size": 5,
                "returned": 5,
                "has_more": True,
                "next_offset": 5,
            },
            "products": [{"order_book_id": str(index)} for index in range(5)],
            "returned": 5,
            "ranking": "two_way_quote, spread_percent_asc, turnover_desc",
        }

    async def fake_enrich(self, *_args, **_kwargs):
        return {
            "products": [],
            "returned": 0,
            "pagination": {
                "total": 300,
                "offset": 0,
                "page_size": 5,
                "returned": 0,
                "has_more": False,
                "next_offset": None,
            },
            "execution_ranking": None,
            "execution_freshness_basis": "bid_ask_updated_at",
            "last_trade_role": "informational_only_for_leveraged_products",
            "enrichment": {
                "authenticated_quote_count": 150,
                "attempted_count": 150,
                "remaining_count": 150,
                "scan_complete": False,
                "batch_size": 150,
            },
        }

    monkeypatch.setattr(LeveragedScreenService, "screen", fake_screen)
    monkeypatch.setattr(LeveragedScreenService, "enrich_snapshot", fake_enrich)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "screen_leveraged_instruments",
            {
                "underlying_order_book_id": "4478",
                "direction": "long",
                "product_types": ["warrant"],
                "page_size": 5,
            },
        )

    payload = json.loads(result.content[0].text)
    assert payload["products"] == []
    assert payload["returned"] == 0
    assert payload["ranking"] is None
    assert payload["execution"]["status"] == "authenticated_partial"
    assert payload["execution"]["enrichment"]["scan_complete"] is False
    assert payload["execution"]["enrichment"]["remaining_count"] == 150
    assert payload["structural_pagination"]["returned"] == 5


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
        "target_leverage",
        "max_leverage_deviation",
        "min_stop_loss_buffer_percent",
        "require_two_way_quote",
        "max_spread_percent",
        "min_turnover",
    ):
        assert field in props

    assert "enrich_leveraged_snapshot" not in tools


@pytest.mark.asyncio
async def test_public_filter_tools_remain_capped_at_100_rows():
    async with Client(mcp) as client:
        tools = {item.name: item for item in await client.list_tools()}

    assert tools["filter_certificates"].input_schema["properties"]["limit"]["maximum"] == 100
    assert tools["filter_warrants"].input_schema["properties"]["limit"]["maximum"] == 100


def test_default_progressive_execution_batch_size_is_300():
    from avanza_mcp.services.leveraged_screen_service import _EXECUTION_BATCH_SIZE

    assert _EXECUTION_BATCH_SIZE == 300


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
