"""Snapshot-backed leveraged-instrument discovery, filtering, and pagination."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from ..client.base import AvanzaClient
from ..client.exceptions import AvanzaError
from ..models.certificate import CertificateFilter, CertificateFilterRequest
from ..models.filter import SortBy
from ..models.warrant import WarrantFilter, WarrantFilterRequest
from .market_data_service import MarketDataService

ProductType = Literal["certificate", "warrant"]
Direction = Literal["long", "short"]
_PAGE_SIZE = 100
_MAX_CONCURRENT_PAGES = 4
_SNAPSHOT_TTL = timedelta(minutes=10)
_RANKING = "two_way_quote, spread_percent_asc, turnover_desc"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _discovery_spread_percent(bid: Any, ask: Any) -> float | None:
    bid_value, ask_value = _number(bid), _number(ask)
    if bid_value is None or ask_value is None or bid_value <= 0 or ask_value <= 0 or ask_value < bid_value:
        return None
    midpoint = (bid_value + ask_value) / 2
    return round((ask_value - bid_value) / midpoint * 100, 6) if midpoint else None


def _normalize_candidate(item: Any, product_type: ProductType) -> dict[str, Any]:
    raw = item.model_dump(mode="json", by_alias=True, exclude_none=True)
    bid, ask = raw.get("buyPrice"), raw.get("sellPrice")
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


def _has_two_way_quote(candidate: dict[str, Any]) -> bool:
    bid = _number(candidate.get("discovery_bid"))
    ask = _number(candidate.get("discovery_ask"))
    return bid is not None and ask is not None and bid > 0 and ask > 0


def _candidate_rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
    spread = _number(candidate.get("spread_percent_from_discovery_prices"))
    turnover = _number(candidate.get("total_value_traded")) or 0.0
    return (
        0 if _has_two_way_quote(candidate) else 1,
        spread if spread is not None else float("inf"),
        -turnover,
        str(candidate.get("product_type") or ""),
        str(candidate.get("issuer") or ""),
        str(candidate.get("name") or ""),
        str(candidate.get("order_book_id") or ""),
    )


@dataclass(frozen=True)
class ScreenFilters:
    issuers: tuple[str, ...] = ()
    sub_types: tuple[str, ...] = ()
    min_leverage: float | None = None
    max_leverage: float | None = None
    require_two_way_quote: bool = False
    max_spread_percent: float | None = None
    min_turnover: float | None = None


def _normalize_filter_values(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({value.strip().casefold() for value in values if value.strip()}))


def _normalize_filters(filters: ScreenFilters) -> ScreenFilters:
    return ScreenFilters(
        issuers=_normalize_filter_values(filters.issuers),
        sub_types=_normalize_filter_values(filters.sub_types),
        min_leverage=filters.min_leverage,
        max_leverage=filters.max_leverage,
        require_two_way_quote=filters.require_two_way_quote,
        max_spread_percent=filters.max_spread_percent,
        min_turnover=filters.min_turnover,
    )


def _validate_filters(filters: ScreenFilters) -> None:
    if filters.min_leverage is not None and filters.min_leverage < 0:
        raise ValueError("min_leverage must be >= 0")
    if filters.max_leverage is not None and filters.max_leverage < 0:
        raise ValueError("max_leverage must be >= 0")
    if (
        filters.min_leverage is not None
        and filters.max_leverage is not None
        and filters.min_leverage > filters.max_leverage
    ):
        raise ValueError("min_leverage must be <= max_leverage")
    if filters.max_spread_percent is not None and filters.max_spread_percent < 0:
        raise ValueError("max_spread_percent must be >= 0")
    if filters.min_turnover is not None and filters.min_turnover < 0:
        raise ValueError("min_turnover must be >= 0")


def _filter_payload(filters: ScreenFilters) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if filters.issuers:
        payload["issuers"] = list(filters.issuers)
    if filters.sub_types:
        payload["sub_types"] = list(filters.sub_types)
    if filters.min_leverage is not None:
        payload["min_leverage"] = filters.min_leverage
    if filters.max_leverage is not None:
        payload["max_leverage"] = filters.max_leverage
    if filters.require_two_way_quote:
        payload["require_two_way_quote"] = True
    if filters.max_spread_percent is not None:
        payload["max_spread_percent"] = filters.max_spread_percent
    if filters.min_turnover is not None:
        payload["min_turnover"] = filters.min_turnover
    return payload


def _matches_filters(candidate: dict[str, Any], filters: ScreenFilters) -> bool:
    if filters.issuers:
        issuer = str(candidate.get("issuer") or "").strip().casefold()
        if issuer not in filters.issuers:
            return False

    if filters.sub_types:
        sub_type = str(candidate.get("sub_type") or "").strip().casefold()
        if sub_type not in filters.sub_types:
            return False

    leverage = _number(candidate.get("leverage"))
    if filters.min_leverage is not None and (leverage is None or leverage < filters.min_leverage):
        return False
    if filters.max_leverage is not None and (leverage is None or leverage > filters.max_leverage):
        return False

    if filters.require_two_way_quote and not _has_two_way_quote(candidate):
        return False

    spread = _number(candidate.get("spread_percent_from_discovery_prices"))
    if filters.max_spread_percent is not None and (
        spread is None or spread > filters.max_spread_percent
    ):
        return False

    turnover = _number(candidate.get("total_value_traded"))
    if filters.min_turnover is not None and (
        turnover is None or turnover < filters.min_turnover
    ):
        return False

    return True


def _display_values(candidates: list[dict[str, Any]], field: str) -> list[str]:
    values: dict[str, str] = {}
    for candidate in candidates:
        raw = candidate.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            values.setdefault(value.casefold(), value)
    return [values[key] for key in sorted(values)]


def _available_filter_values(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    leverage_values = [
        value
        for candidate in candidates
        if (value := _number(candidate.get("leverage"))) is not None
    ]
    return {
        "issuers": _display_values(candidates, "issuer"),
        "sub_types": _display_values(candidates, "sub_type"),
        "leverage": {
            "reported_count": len(leverage_values),
            "min": min(leverage_values) if leverage_values else None,
            "max": max(leverage_values) if leverage_values else None,
        },
    }


@dataclass(frozen=True)
class _Snapshot:
    snapshot_id: str
    underlying_order_book_id: str
    direction: Direction
    product_types: tuple[ProductType, ...]
    filters: ScreenFilters
    available_filter_values: dict[str, Any]
    families: dict[str, dict[str, Any]]
    products: list[dict[str, Any]]
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    scanned_count: int
    quote_complete_count: int
    eligible_quote_complete_count: int
    expires_at: datetime


class _SnapshotStore:
    def __init__(self) -> None:
        self._data: dict[str, _Snapshot] = {}
        self._lock = Lock()

    def put(self, snapshot: _Snapshot) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._data = {
                key: value
                for key, value in self._data.items()
                if value.expires_at > now
            }
            self._data[snapshot.snapshot_id] = snapshot

    def get(self, snapshot_id: str) -> _Snapshot:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._data = {
                key: value
                for key, value in self._data.items()
                if value.expires_at > now
            }
            snapshot = self._data.get(snapshot_id)
        if snapshot is None:
            raise ValueError(
                "snapshot_id was not found or has expired; create a new leveraged screen snapshot"
            )
        return snapshot


_SNAPSHOTS = _SnapshotStore()


def _page(snapshot: _Snapshot, offset: int, page_size: int) -> dict[str, Any]:
    total = len(snapshot.products)
    products = snapshot.products[offset : offset + page_size]
    returned = len(products)
    has_more = offset + returned < total
    return {
        "snapshot_id": snapshot.snapshot_id,
        "underlying_order_book_id": snapshot.underlying_order_book_id,
        "direction": snapshot.direction,
        "product_types": list(snapshot.product_types),
        "filters": _filter_payload(snapshot.filters),
        "available_filter_values": snapshot.available_filter_values,
        "families": snapshot.families,
        "snapshot": {
            "started_at": snapshot.started_at.isoformat(),
            "completed_at": snapshot.completed_at.isoformat(),
            "duration_ms": snapshot.duration_ms,
            "scanned_count": snapshot.scanned_count,
            "eligible_count": total,
            "quote_complete_count": snapshot.quote_complete_count,
            "eligible_quote_complete_count": snapshot.eligible_quote_complete_count,
            "atomic": False,
            "comparison_complete": all(
                "error" not in family for family in snapshot.families.values()
            ),
            "expires_at": snapshot.expires_at.isoformat(),
        },
        "pagination": {
            "total": total,
            "offset": offset,
            "page_size": page_size,
            "returned": returned,
            "has_more": has_more,
            "next_offset": offset + returned if has_more else None,
        },
        "products": products,
        "returned": returned,
        "ranking": _RANKING,
        "data_note": (
            "The complete underlying/direction/product-family universe was scanned once, then "
            "the reported filters were applied before ranking. pagination.total is the eligible "
            "filtered count; snapshot.scanned_count is the full scanned count. "
            "available_filter_values comes from the full scanned universe. Calls using "
            "snapshot_id reuse the frozen ranking and do not refetch market data. Initial quote "
            "collection is non-atomic because upstream pages are fetched over time; after the first "
            "page establishes the total, remaining pages may be fetched concurrently. If "
            "pagination.has_more is true, this response is only a partial view of the snapshot."
        ),
    }


class LeveragedScreenService:
    def __init__(self, client: AvanzaClient) -> None:
        self._market = MarketDataService(client)

    async def _collect_certificates(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        _legacy_limit: int | None = None,
    ) -> dict[str, Any]:
        async def fetch(offset: int):
            return await self._market.filter_certificates(
                CertificateFilterRequest(
                    filter=CertificateFilter(
                        directions=[direction],
                        underlyingInstruments=[underlying_order_book_id],
                    ),
                    offset=offset,
                    limit=_PAGE_SIZE,
                    sortBy=SortBy(field="name", order="asc"),
                )
            )

        first = await fetch(0)
        items: list[Any] = list(first.certificates)
        total = first.totalNumberOfOrderbooks

        if total is not None and len(first.certificates) == _PAGE_SIZE:
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

            async def fetch_bounded(offset: int):
                async with semaphore:
                    return await fetch(offset)

            offsets = list(range(_PAGE_SIZE, total, _PAGE_SIZE))
            if offsets:
                responses = await asyncio.gather(
                    *(fetch_bounded(offset) for offset in offsets)
                )
                for response in responses:
                    items.extend(response.certificates)
        else:
            offset = len(first.certificates)
            while first.certificates and (total is None or offset < total):
                response = await fetch(offset)
                page = response.certificates
                if not page:
                    break
                items.extend(page)
                if response.totalNumberOfOrderbooks is not None:
                    total = response.totalNumberOfOrderbooks
                offset += len(page)
                if len(page) < _PAGE_SIZE:
                    break
        products = [_normalize_candidate(item, "certificate") for item in items]
        return {
            "products": products,
            "upstream_total": total,
            "scanned_count": len(items),
            "quote_complete_count": sum(
                _has_two_way_quote(item) for item in products
            ),
        }

    async def _collect_warrants(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        _legacy_limit: int | None = None,
    ) -> dict[str, Any]:
        async def fetch(offset: int):
            return await self._market.filter_warrants(
                WarrantFilterRequest(
                    filter=WarrantFilter(
                        directions=[direction],
                        underlyingInstruments=[underlying_order_book_id],
                    ),
                    offset=offset,
                    limit=_PAGE_SIZE,
                    sortBy=SortBy(field="name", order="asc"),
                )
            )

        first = await fetch(0)
        items: list[Any] = list(first.warrants)
        total = first.totalNumberOfOrderbooks

        if total is not None and len(first.warrants) == _PAGE_SIZE:
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

            async def fetch_bounded(offset: int):
                async with semaphore:
                    return await fetch(offset)

            offsets = list(range(_PAGE_SIZE, total, _PAGE_SIZE))
            if offsets:
                responses = await asyncio.gather(
                    *(fetch_bounded(offset) for offset in offsets)
                )
                for response in responses:
                    items.extend(response.warrants)
        else:
            offset = len(first.warrants)
            while first.warrants and (total is None or offset < total):
                response = await fetch(offset)
                page = response.warrants
                if not page:
                    break
                items.extend(page)
                if response.totalNumberOfOrderbooks is not None:
                    total = response.totalNumberOfOrderbooks
                offset += len(page)
                if len(page) < _PAGE_SIZE:
                    break
        products = [_normalize_candidate(item, "warrant") for item in items]
        return {
            "products": products,
            "upstream_total": total,
            "scanned_count": len(items),
            "quote_complete_count": sum(
                _has_two_way_quote(item) for item in products
            ),
        }

    async def screen(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        product_types: list[ProductType],
        page_size: int,
        filters: ScreenFilters | None = None,
    ) -> dict[str, Any]:
        if not product_types or len(set(product_types)) != len(product_types):
            raise ValueError("product_types must contain unique product types")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        selected_filters = _normalize_filters(filters or ScreenFilters())
        _validate_filters(selected_filters)

        started_at, timer = datetime.now(timezone.utc), perf_counter()
        collectors = {
            "certificate": self._collect_certificates,
            "warrant": self._collect_warrants,
        }
        results = await asyncio.gather(
            *(
                collectors[product_type](underlying_order_book_id, direction)
                for product_type in product_types
            ),
            return_exceptions=True,
        )
        completed_at = datetime.now(timezone.utc)

        families: dict[str, dict[str, Any]] = {}
        all_products: list[dict[str, Any]] = []
        scanned_count = quote_complete_count = 0
        for product_type, result in zip(product_types, results, strict=True):
            if isinstance(result, Exception):
                families[product_type] = {
                    "upstream_total": None,
                    "scanned_count": 0,
                    "quote_complete_count": 0,
                    "eligible_count": 0,
                    "error": (
                        "upstream_unavailable"
                        if isinstance(result, AvanzaError)
                        else "screen_failed"
                    ),
                }
                continue
            families[product_type] = {
                key: result[key]
                for key in (
                    "upstream_total",
                    "scanned_count",
                    "quote_complete_count",
                )
            }
            scanned_count += result["scanned_count"]
            quote_complete_count += result["quote_complete_count"]
            all_products.extend(result["products"])

        available_filter_values = _available_filter_values(all_products)
        products = [
            candidate
            for candidate in all_products
            if _matches_filters(candidate, selected_filters)
        ]
        for product_type in product_types:
            families[product_type]["eligible_count"] = sum(
                candidate.get("product_type") == product_type
                for candidate in products
            )

        products.sort(key=_candidate_rank)
        eligible_quote_complete_count = sum(
            _has_two_way_quote(candidate) for candidate in products
        )
        snapshot = _Snapshot(
            uuid4().hex,
            underlying_order_book_id,
            direction,
            tuple(product_types),
            selected_filters,
            available_filter_values,
            families,
            products,
            started_at,
            completed_at,
            round((perf_counter() - timer) * 1000, 3),
            scanned_count,
            quote_complete_count,
            eligible_quote_complete_count,
            completed_at + _SNAPSHOT_TTL,
        )
        _SNAPSHOTS.put(snapshot)
        return _page(snapshot, 0, page_size)

    def get_page(
        self,
        snapshot_id: str,
        underlying_order_book_id: str,
        direction: Direction,
        offset: int,
        page_size: int,
        product_types: list[ProductType] | None = None,
        filters: ScreenFilters | None = None,
    ) -> dict[str, Any]:
        if offset < 0 or page_size < 1:
            raise ValueError("offset must be >= 0 and page_size must be >= 1")
        if filters is not None:
            filters = _normalize_filters(filters)
            _validate_filters(filters)

        snapshot = _SNAPSHOTS.get(snapshot_id)
        if (
            snapshot.underlying_order_book_id != underlying_order_book_id
            or snapshot.direction != direction
        ):
            raise ValueError(
                "snapshot_id does not match the supplied underlying_order_book_id and direction"
            )
        if product_types is not None and set(product_types) != set(snapshot.product_types):
            raise ValueError("product_types do not match the stored snapshot")
        if filters is not None and filters != snapshot.filters:
            raise ValueError("filters do not match the stored snapshot")
        return _page(snapshot, offset, page_size)
