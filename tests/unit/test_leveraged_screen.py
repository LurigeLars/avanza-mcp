from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp import Client

from avanza_mcp import mcp
from avanza_mcp.services.leveraged_screen_service import LeveragedScreenService, _discovery_spread_percent


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
            certificates=[FakeItem(orderbookId="101", name="MINI L TEST", direction="long", issuer="Issuer A", leverage=4.2, buyPrice=9.9, sellPrice=10.1, totalValueTraded=123456)],
            totalNumberOfOrderbooks=1,
        )

    async def filter_warrants(self, request):
        self.warrant_calls.append(request)
        return SimpleNamespace(
            warrants=[FakeItem(orderbookId="202", name="TURBO L TEST", direction="long", issuer="Issuer B", subType="TURBO", buyPrice=4.95, sellPrice=5.05, totalValueTraded=654321)],
            totalNumberOfOrderbooks=1,
        )


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


class PaginatedWarrantMarket:
    def __init__(self):
        self.warrant_calls = []

    async def filter_warrants(self, request):
        self.warrant_calls.append(request)
        if request.offset == 0:
            return SimpleNamespace(
                warrants=[FakeItem(orderbookId=str(i), name=f"EARLY {i:03d}", direction="long", issuer="Issuer A", buyPrice=10.0, sellPrice=10.5, totalValueTraded=0) for i in range(100)],
                totalNumberOfOrderbooks=101,
            )
        return SimpleNamespace(
            warrants=[FakeItem(orderbookId="999", name="LATE BEST", direction="long", issuer="Issuer B", buyPrice=10.0, sellPrice=10.01, totalValueTraded=1_000_000)],
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
async def test_screen_tool_has_unbounded_page_size_and_snapshot_contract():
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
