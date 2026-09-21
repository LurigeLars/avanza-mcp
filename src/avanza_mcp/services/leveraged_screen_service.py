"""Bounded server-side aggregation for leveraged instrument discovery."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from ..client.base import AvanzaClient
from ..client.exceptions import AvanzaError
from ..models.certificate import CertificateFilter, CertificateFilterRequest
from ..models.filter import SortBy
from ..models.warrant import WarrantFilter, WarrantFilterRequest
from .market_data_service import MarketDataService

ProductType = Literal["certificate", "warrant"]
Direction = Literal["long", "short"]

_PAGE_SIZE = 100
_MAX_PER_TYPE = 200


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _discovery_spread_percent(bid: Any, ask: Any) -> float | None:
    """Calculate a comparable spread from discovery bid/ask without implying executability."""
    bid_value = _number(bid)
    ask_value = _number(ask)
    if (
        bid_value is None
        or ask_value is None
        or bid_value <= 0
        or ask_value <= 0
        or ask_value < bid_value
    ):
        return None
    midpoint = (bid_value + ask_value) / 2
    if midpoint == 0:
        return None
    return round((ask_value - bid_value) / midpoint * 100, 6)


def _normalize_candidate(item: Any, product_type: ProductType) -> dict[str, Any]:
    raw = item.model_dump(mode="json", by_alias=True, exclude_none=True)
    bid = raw.get("buyPrice")
    ask = raw.get("sellPrice")
    underlying = raw.get("underlyingInstrument")
    if isinstance(underlying, dict):
        underlying = {
            key: underlying[key]
            for key in ("orderbookId", "name", "instrumentType", "countryCode")
            if underlying.get(key) is not None
        }

    return {
        key: value
        for key, value in {
            "product_type": product_type,
            "order_book_id": raw.get("orderbookId"),
            "name": raw.get("name"),
            "direction": raw.get("direction"),
            "issuer": raw.get("issuer"),
            "sub_type": raw.get("subType"),
            "leverage": raw.get("leverage"),
            "stop_loss": raw.get("stopLoss"),
            "discovery_bid": bid,
            "discovery_ask": ask,
            "upstream_spread": raw.get("spread"),
            "spread_percent_from_discovery_prices": _discovery_spread_percent(bid, ask),
            "total_value_traded": raw.get("totalValueTraded"),
            "underlying": underlying,
        }.items()
        if value is not None
    }


class LeveragedScreenService:
    """Aggregate bounded certificate/warrant screens inside one MCP tool call."""

    def __init__(self, client: AvanzaClient) -> None:
        self._market = MarketDataService(client)

    async def _collect_certificates(
        self, underlying_order_book_id: str, direction: Direction, max_results: int
    ) -> dict[str, Any]:
        items: list[Any] = []
        offset = 0
        total: int | None = None
        while len(items) < max_results:
            limit = min(_PAGE_SIZE, max_results - len(items))
            response = await self._market.filter_certificates(
                CertificateFilterRequest(
                    filter=CertificateFilter(
                        directions=[direction],
                        underlyingInstruments=[underlying_order_book_id],
                    ),
                    offset=offset,
                    limit=limit,
                    sortBy=SortBy(field="name", order="asc"),
                )
            )
            page = response.certificates
            items.extend(page)
            total = response.totalNumberOfOrderbooks
            offset += len(page)
            if not page or len(page) < limit or (total is not None and offset >= total):
                break
        return {
            "products": [_normalize_candidate(item, "certificate") for item in items],
            "upstream_total": total,
            "returned": len(items),
            "truncated": total is not None and len(items) < total,
        }

    async def _collect_warrants(
        self, underlying_order_book_id: str, direction: Direction, max_results: int
    ) -> dict[str, Any]:
        items: list[Any] = []
        offset = 0
        total: int | None = None
        while len(items) < max_results:
            limit = min(_PAGE_SIZE, max_results - len(items))
            response = await self._market.filter_warrants(
                WarrantFilterRequest(
                    filter=WarrantFilter(
                        directions=[direction],
                        underlyingInstruments=[underlying_order_book_id],
                    ),
                    offset=offset,
                    limit=limit,
                    sortBy=SortBy(field="name", order="asc"),
                )
            )
            page = response.warrants
            items.extend(page)
            total = response.totalNumberOfOrderbooks
            offset += len(page)
            if not page or len(page) < limit or (total is not None and offset >= total):
                break
        return {
            "products": [_normalize_candidate(item, "warrant") for item in items],
            "upstream_total": total,
            "returned": len(items),
            "truncated": total is not None and len(items) < total,
        }

    async def screen(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        product_types: list[ProductType],
        max_per_type: int,
    ) -> dict[str, Any]:
        if not product_types:
            raise ValueError("product_types must contain at least one product type")
        if len(set(product_types)) != len(product_types):
            raise ValueError("product_types must not contain duplicates")
        if not 1 <= max_per_type <= _MAX_PER_TYPE:
            raise ValueError(f"max_per_type must be between 1 and {_MAX_PER_TYPE}")

        collectors = {
            "certificate": self._collect_certificates,
            "warrant": self._collect_warrants,
        }
        tasks = [
            collectors[product_type](
                underlying_order_book_id, direction, max_per_type
            )
            for product_type in product_types
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        families: dict[str, dict[str, Any]] = {}
        products: list[dict[str, Any]] = []
        for product_type, result in zip(product_types, results, strict=True):
            if isinstance(result, Exception):
                families[product_type] = {
                    "returned": 0,
                    "upstream_total": None,
                    "truncated": False,
                    "error": (
                        "upstream_unavailable"
                        if isinstance(result, AvanzaError)
                        else "screen_failed"
                    ),
                }
                continue
            families[product_type] = {
                key: result[key]
                for key in ("returned", "upstream_total", "truncated")
            }
            products.extend(result["products"])

        return {
            "underlying_order_book_id": underlying_order_book_id,
            "direction": direction,
            "families": families,
            "products": products,
            "returned": len(products),
            "data_note": (
                "Discovery/filter snapshot only. Prices, spread and turnover may be stale "
                "or absent outside market hours and are not execution-verified."
            ),
        }
