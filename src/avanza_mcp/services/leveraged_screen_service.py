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


def _candidate_rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
    """Rank discovery candidates by observable liquidity quality, then stable identity.

    This is a discovery ranking only. It prefers complete two-way quotes, tighter
    displayed spreads and higher observed turnover. It does not imply trade quality.
    """
    bid = _number(candidate.get("discovery_bid"))
    ask = _number(candidate.get("discovery_ask"))
    has_two_way_quote = bid is not None and ask is not None and bid > 0 and ask > 0
    spread = _number(candidate.get("spread_percent_from_discovery_prices"))
    turnover = _number(candidate.get("total_value_traded")) or 0.0
    return (
        0 if has_two_way_quote else 1,
        spread if spread is not None else float("inf"),
        -turnover,
        str(candidate.get("issuer") or ""),
        str(candidate.get("name") or ""),
        str(candidate.get("order_book_id") or ""),
    )


def _rank_and_limit(
    items: list[Any], product_type: ProductType, max_results: int
) -> list[dict[str, Any]]:
    candidates = [_normalize_candidate(item, product_type) for item in items]
    candidates.sort(key=_candidate_rank)
    return candidates[:max_results]


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
        while True:
            limit = _PAGE_SIZE
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
        products = _rank_and_limit(items, "certificate", max_results)
        return {
            "products": products,
            "upstream_total": total,
            "scanned": len(items),
            "returned": len(products),
            "truncated": total is not None and len(products) < total,
        }

    async def _collect_warrants(
        self, underlying_order_book_id: str, direction: Direction, max_results: int
    ) -> dict[str, Any]:
        items: list[Any] = []
        offset = 0
        total: int | None = None
        while True:
            limit = _PAGE_SIZE
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
        products = _rank_and_limit(items, "warrant", max_results)
        return {
            "products": products,
            "upstream_total": total,
            "scanned": len(items),
            "returned": len(products),
            "truncated": total is not None and len(products) < total,
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
                for key in ("returned", "upstream_total", "scanned", "truncated")
            }
            products.extend(result["products"])

        return {
            "underlying_order_book_id": underlying_order_book_id,
            "direction": direction,
            "families": families,
            "products": products,
            "returned": len(products),
            "ranking": "two_way_quote, spread_percent_asc, turnover_desc",
            "data_note": (
                "Discovery/filter snapshot only. Prices, spread and turnover may be stale "
                "or absent outside market hours and are not execution-verified."
            ),
        }
