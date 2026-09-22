from unittest.mock import AsyncMock

import pytest

from avanza_mcp.client.exceptions import AvanzaNotFoundError, AvanzaRateLimitError
from avanza_mcp.services.market_data_service import MarketDataService


OPTION_INFO = {
    "orderbookId": "2401349",
    "name": "VOLVB6L137.59Z",
    "isin": "SE0028422279",
    "tradable": "BUYABLE_AND_SELLABLE",
    "keyIndicators": {
        "callIndicator": "Köp",
        "endDate": "2026-12-18",
        "parity": 109,
        "strikePrice": 137.59,
        "subType": "STANDARD",
    },
    "quote": {
        "last": 194.4,
        "isRealTime": False,
    },
    "type": "OPTION",
    "underlying": {
        "orderbookId": "5269",
        "name": "Volvo B",
        "instrumentType": "STOCK",
    },
}

OPTION_DETAILS = {
    "underlying": {
        "orderbookId": "5269",
        "name": "Volvo B",
        "instrumentType": "STOCK",
    },
    "orderDepth": {"receivedTime": 123, "levels": []},
    "trades": [],
    "tradingUnit": 1,
    "exerciseType": "Amerikansk",
}


@pytest.mark.asyncio
async def test_future_forward_info_falls_back_to_option_only_on_not_found():
    client = AsyncMock()
    client.get.side_effect = [AvanzaNotFoundError(), OPTION_INFO]
    service = MarketDataService(client)

    result = await service.get_future_forward_info("2401349")

    assert result.orderbookId == "2401349"
    assert result.type == "OPTION"
    assert result.keyIndicators["endDate"] == "2026-12-18"
    assert [call.args[0] for call in client.get.await_args_list] == [
        "/_api/market-guide/futureforward/2401349",
        "/_api/market-guide/option/2401349",
    ]


@pytest.mark.asyncio
async def test_future_forward_details_falls_back_to_option_only_on_not_found():
    client = AsyncMock()
    client.get.side_effect = [AvanzaNotFoundError(), OPTION_DETAILS]
    service = MarketDataService(client)

    result = await service.get_future_forward_details("2401349")
    payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload["exerciseType"] == "Amerikansk"
    assert payload["orderDepth"]["levels"] == []
    assert [call.args[0] for call in client.get.await_args_list] == [
        "/_api/market-guide/futureforward/2401349/details",
        "/_api/market-guide/option/2401349/details",
    ]


@pytest.mark.asyncio
async def test_future_forward_info_does_not_fallback_on_rate_limit():
    client = AsyncMock()
    client.get.side_effect = AvanzaRateLimitError()
    service = MarketDataService(client)

    with pytest.raises(AvanzaRateLimitError):
        await service.get_future_forward_info("2401349")

    client.get.assert_awaited_once_with(
        "/_api/market-guide/futureforward/2401349"
    )


@pytest.mark.asyncio
async def test_future_forward_success_does_not_probe_option_path():
    client = AsyncMock()
    client.get.return_value = {
        "orderbookId": "123",
        "name": "FUTURE TEST",
        "type": "FUTURE_FORWARD",
    }
    service = MarketDataService(client)

    result = await service.get_future_forward_info("123")

    assert result.orderbookId == "123"
    client.get.assert_awaited_once_with("/_api/market-guide/futureforward/123")


@pytest.mark.asyncio
async def test_direct_option_info_uses_option_path_without_future_probe():
    client = AsyncMock()
    client.get.return_value = OPTION_INFO
    service = MarketDataService(client)

    result = await service.get_option_info("2401349")

    assert result.orderbookId == "2401349"
    assert result.type == "OPTION"
    client.get.assert_awaited_once_with("/_api/market-guide/option/2401349")
