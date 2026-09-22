"""Snapshot-backed leveraged-instrument discovery and pagination."""
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
        underlying = {k: underlying[k] for k in ("orderbookId", "name", "instrumentType", "countryCode") if underlying.get(k) is not None}
    return {k: v for k, v in {
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
    }.items() if v is not None}


def _has_two_way_quote(candidate: dict[str, Any]) -> bool:
    bid, ask = _number(candidate.get("discovery_bid")), _number(candidate.get("discovery_ask"))
    return bid is not None and ask is not None and bid > 0 and ask > 0


def _candidate_rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
    spread = _number(candidate.get("spread_percent_from_discovery_prices"))
    turnover = _number(candidate.get("total_value_traded")) or 0.0
    return (0 if _has_two_way_quote(candidate) else 1, spread if spread is not None else float("inf"), -turnover, str(candidate.get("product_type") or ""), str(candidate.get("issuer") or ""), str(candidate.get("name") or ""), str(candidate.get("order_book_id") or ""))


@dataclass(frozen=True)
class _Snapshot:
    snapshot_id: str
    underlying_order_book_id: str
    direction: Direction
    product_types: tuple[ProductType, ...]
    families: dict[str, dict[str, Any]]
    products: list[dict[str, Any]]
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    scanned_count: int
    quote_complete_count: int
    expires_at: datetime


class _SnapshotStore:
    def __init__(self) -> None:
        self._data: dict[str, _Snapshot] = {}
        self._lock = Lock()

    def put(self, snapshot: _Snapshot) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._data = {k: v for k, v in self._data.items() if v.expires_at > now}
            self._data[snapshot.snapshot_id] = snapshot

    def get(self, snapshot_id: str) -> _Snapshot:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._data = {k: v for k, v in self._data.items() if v.expires_at > now}
            snapshot = self._data.get(snapshot_id)
        if snapshot is None:
            raise ValueError("snapshot_id was not found or has expired; create a new leveraged screen snapshot")
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
        "families": snapshot.families,
        "snapshot": {
            "started_at": snapshot.started_at.isoformat(),
            "completed_at": snapshot.completed_at.isoformat(),
            "duration_ms": snapshot.duration_ms,
            "scanned_count": snapshot.scanned_count,
            "quote_complete_count": snapshot.quote_complete_count,
            "atomic": False,
            "comparison_complete": True,
            "expires_at": snapshot.expires_at.isoformat(),
        },
        "pagination": {
            "total": total,
            "offset": offset,
            "page_size": page_size,
            "returned": returned,
            "has_more": has_more,
            "next_offset": offset + returned if has_more else None,
            "complete_result_set": offset == 0 and returned == total,
        },
        "products": products,
        "returned": returned,
        "ranking": _RANKING,
        "data_note": (
            "The complete matching universe was ranked once when this snapshot was created. "
            "Calls using snapshot_id reuse the frozen ranking and do not refetch market data. "
            "Initial quote collection is non-atomic because upstream pages are sequential. "
            "If pagination.has_more is true, this response is only a partial view of the snapshot."
        ),
    }


class LeveragedScreenService:
    def __init__(self, client: AvanzaClient) -> None:
        self._market = MarketDataService(client)

    async def _collect_certificates(self, underlying_order_book_id: str, direction: Direction, _legacy_limit: int | None = None) -> dict[str, Any]:
        items: list[Any] = []
        offset, total = 0, None
        while True:
            response = await self._market.filter_certificates(CertificateFilterRequest(filter=CertificateFilter(directions=[direction], underlyingInstruments=[underlying_order_book_id]), offset=offset, limit=_PAGE_SIZE, sortBy=SortBy(field="name", order="asc")))
            page = response.certificates
            items.extend(page)
            total = response.totalNumberOfOrderbooks
            offset += len(page)
            if not page or len(page) < _PAGE_SIZE or (total is not None and offset >= total):
                break
        products = [_normalize_candidate(item, "certificate") for item in items]
        return {"products": products, "upstream_total": total, "scanned_count": len(items), "quote_complete_count": sum(_has_two_way_quote(item) for item in products)}

    async def _collect_warrants(self, underlying_order_book_id: str, direction: Direction, _legacy_limit: int | None = None) -> dict[str, Any]:
        items: list[Any] = []
        offset, total = 0, None
        while True:
            response = await self._market.filter_warrants(WarrantFilterRequest(filter=WarrantFilter(directions=[direction], underlyingInstruments=[underlying_order_book_id]), offset=offset, limit=_PAGE_SIZE, sortBy=SortBy(field="name", order="asc")))
            page = response.warrants
            items.extend(page)
            total = response.totalNumberOfOrderbooks
            offset += len(page)
            if not page or len(page) < _PAGE_SIZE or (total is not None and offset >= total):
                break
        products = [_normalize_candidate(item, "warrant") for item in items]
        return {"products": products, "upstream_total": total, "scanned_count": len(items), "quote_complete_count": sum(_has_two_way_quote(item) for item in products)}

    async def screen(self, underlying_order_book_id: str, direction: Direction, product_types: list[ProductType], page_size: int) -> dict[str, Any]:
        if not product_types or len(set(product_types)) != len(product_types):
            raise ValueError("product_types must contain unique product types")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        started_at, timer = datetime.now(timezone.utc), perf_counter()
        collectors = {"certificate": self._collect_certificates, "warrant": self._collect_warrants}
        results = await asyncio.gather(*(collectors[t](underlying_order_book_id, direction) for t in product_types), return_exceptions=True)
        completed_at = datetime.now(timezone.utc)

        families: dict[str, dict[str, Any]] = {}
        products: list[dict[str, Any]] = []
        scanned_count = quote_complete_count = 0
        for product_type, result in zip(product_types, results, strict=True):
            if isinstance(result, Exception):
                families[product_type] = {"upstream_total": None, "scanned_count": 0, "quote_complete_count": 0, "error": "upstream_unavailable" if isinstance(result, AvanzaError) else "screen_failed"}
                continue
            families[product_type] = {k: result[k] for k in ("upstream_total", "scanned_count", "quote_complete_count")}
            scanned_count += result["scanned_count"]
            quote_complete_count += result["quote_complete_count"]
            products.extend(result["products"])

        products.sort(key=_candidate_rank)
        snapshot = _Snapshot(uuid4().hex, underlying_order_book_id, direction, tuple(product_types), families, products, started_at, completed_at, round((perf_counter() - timer) * 1000, 3), scanned_count, quote_complete_count, completed_at + _SNAPSHOT_TTL)
        _SNAPSHOTS.put(snapshot)
        return _page(snapshot, 0, page_size)

    def get_page(self, snapshot_id: str, underlying_order_book_id: str, direction: Direction, offset: int, page_size: int) -> dict[str, Any]:
        if offset < 0 or page_size < 1:
            raise ValueError("offset must be >= 0 and page_size must be >= 1")
        snapshot = _SNAPSHOTS.get(snapshot_id)
        if snapshot.underlying_order_book_id != underlying_order_book_id or snapshot.direction != direction:
            raise ValueError("snapshot_id does not match the supplied underlying_order_book_id and direction")
        return _page(snapshot, offset, page_size)
