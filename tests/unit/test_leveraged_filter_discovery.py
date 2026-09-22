from types import SimpleNamespace

import pytest

from avanza_mcp.services.leveraged_screen_service import (
    LeveragedScreenService,
    ScreenFilters,
)


class FakeItem:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **_kwargs):
        return dict(self.data)


class FakeMarket:
    async def filter_warrants(self, request):
        return SimpleNamespace(
            warrants=[
                FakeItem(
                    orderbookId="1",
                    name="A",
                    direction="long",
                    issuer="Morgan Stanley",
                    subType="MINI_FUTURE",
                    leverage=4.5,
                    buyPrice=10,
                    sellPrice=10.1,
                ),
                FakeItem(
                    orderbookId="2",
                    name="B",
                    direction="long",
                    issuer="Nordea",
                    subType="KNOCK_OUT",
                    leverage=7.0,
                    buyPrice=5,
                    sellPrice=5.1,
                ),
            ],
            totalNumberOfOrderbooks=2,
        )


@pytest.mark.asyncio
async def test_available_filter_values_come_from_full_scanned_universe():
    service = LeveragedScreenService(object())
    service._market = FakeMarket()

    result = await service.screen(
        "4478",
        "long",
        ["warrant"],
        10,
        ScreenFilters(issuers=("morgan stanley",)),
    )

    assert result["pagination"]["total"] == 1
    assert result["available_filter_values"] == {
        "issuers": ["Morgan Stanley", "Nordea"],
        "sub_types": ["KNOCK_OUT", "MINI_FUTURE"],
        "leverage": {"reported_count": 2, "min": 4.5, "max": 7.0},
    }


@pytest.mark.asyncio
async def test_snapshot_filter_comparison_is_case_order_and_duplicate_insensitive():
    service = LeveragedScreenService(object())
    service._market = FakeMarket()

    first = await service.screen(
        "4478",
        "long",
        ["warrant"],
        10,
        ScreenFilters(
            issuers=("Morgan Stanley", "morgan stanley"),
            sub_types=("MINI_FUTURE",),
        ),
    )

    continued = service.get_page(
        first["snapshot_id"],
        "4478",
        "long",
        0,
        10,
        filters=ScreenFilters(
            issuers=("MORGAN STANLEY",),
            sub_types=("mini_future",),
        ),
    )

    assert continued["filters"] == {
        "issuers": ["morgan stanley"],
        "sub_types": ["mini_future"],
    }
