"""Snapshot-backed leveraged-instrument discovery, filtering, and pagination."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field

from ..client.base import AvanzaClient
from ..client.exceptions import AvanzaAuthError, AvanzaError, AvanzaNotFoundError
from ..instrument_catalog import InstrumentCatalog, fresh_default_instrument_catalog
from ..models.certificate import CertificateFilter, CertificateFilterRequest
from ..models.filter import SortBy
from ..models.warrant import WarrantFilter, WarrantFilterRequest
from .market_data_service import MarketDataService

ProductType = Literal["certificate", "warrant"]
Direction = Literal["long", "short"]
_FALLBACK_REQUEST_SIZE = 500
_MAX_CONCURRENT_PAGES = 8
_SNAPSHOT_TTL = timedelta(minutes=10)
_RANKING = "two_way_quote, spread_percent_asc, turnover_desc"
_EXECUTION_RANKING = (
    "fresh_two_way_quote, spread_percent_asc, bid_ask_age_ms_asc, turnover_desc"
)
_EXECUTION_STALE_AFTER_MS = 30_000
_AVANZA_MARKET_TIMEZONE = ZoneInfo("Europe/Stockholm")


class _LeveragedCertificateFilterRequest(CertificateFilterRequest):
    """Internal larger page request; public filter tools remain capped at 100."""

    limit: int = Field(default=20, ge=1)


class _LeveragedWarrantFilterRequest(WarrantFilterRequest):
    """Internal larger page request; public filter tools remain capped at 100."""

    limit: int = Field(default=20, ge=1)


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



def _catalog_candidate(row: dict[str, Any]) -> dict[str, Any]:
    underlying = {
        key: value
        for key, source_key in (
            ("orderbookId", "underlying_order_book_id"),
            ("name", "underlying_name"),
            ("instrumentType", "underlying_instrument_type"),
            ("countryCode", "underlying_country_code"),
        )
        if (value := row.get(source_key)) is not None
    }
    return {
        key: value
        for key, value in {
            "product_type": row.get("product_type"),
            "order_book_id": row.get("order_book_id"),
            "name": row.get("name"),
            "direction": row.get("direction"),
            "issuer": row.get("issuer"),
            "sub_type": row.get("sub_type"),
            "leverage": row.get("leverage"),
            "stop_loss": row.get("stop_loss"),
            "underlying": underlying or None,
        }.items()
        if value is not None
    }


def _catalog_can_satisfy_filters(filters: "ScreenFilters") -> bool:
    # Quote/spread/turnover filters require current market data. Keep the existing
    # public filter-feed path for those so unauthenticated behavior does not regress.
    return not (
        filters.require_two_way_quote
        or filters.max_spread_percent is not None
        or filters.min_turnover is not None
    )

def _timestamp_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Avanza trading-critical timestamps are local Swedish market time
        # without an explicit offset. Resolve them with Europe/Stockholm so
        # daylight-saving transitions are handled correctly.
        parsed = parsed.replace(tzinfo=_AVANZA_MARKET_TIMEZONE)
    return int(parsed.timestamp() * 1000)


def _age_ms(observed_at: int, source_at: int | None) -> int | None:
    return None if source_at is None else max(0, observed_at - source_at)


def _compact_live_quote(quote: dict[str, Any]) -> dict[str, Any]:
    bid, ask = _number(quote.get("buy")), _number(quote.get("sell"))
    observed_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    source_updated_at = _timestamp_ms(quote.get("updated"))
    last_trade_at = _timestamp_ms(quote.get("timeOfLast"))
    bid_ask_age_ms = _age_ms(observed_at, source_updated_at)
    has_two_way_quote = (
        bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
    )
    execution_is_fresh = (
        has_two_way_quote
        and bid_ask_age_ms is not None
        and bid_ask_age_ms <= _EXECUTION_STALE_AFTER_MS
    )
    freshness = {
        key: value
        for key, value in {
            "observed_at": observed_at,
            "source_updated_at": source_updated_at,
            "bid_ask_updated_at": source_updated_at,
            "last_trade_at": last_trade_at,
            "source_update_age_ms": _age_ms(observed_at, source_updated_at),
            "bid_ask_age_ms": bid_ask_age_ms,
            "last_trade_age_ms": _age_ms(observed_at, last_trade_at),
            "execution_freshness_basis": "bid_ask_updated_at",
            "execution_stale_after_ms": _EXECUTION_STALE_AFTER_MS,
            "execution_is_fresh": execution_is_fresh,
            "last_trade_role": "informational_only_for_leveraged_products",
        }.items()
        if value is not None
    }
    compact = {
        key: value
        for key, value in {
            "bid": bid,
            "ask": ask,
            "last": _number(quote.get("last")),
            "highest": _number(quote.get("highest")),
            "lowest": _number(quote.get("lowest")),
            "change": _number(quote.get("change")),
            "change_percent": _number(quote.get("changePercent")),
            "spread_percent_from_live_prices": _discovery_spread_percent(bid, ask),
            "total_value_traded": _number(quote.get("totalValueTraded")),
            "total_volume_traded": _number(quote.get("totalVolumeTraded")),
            "updated": quote.get("updated"),
            "time_of_last": quote.get("timeOfLast"),
            "source": "authenticated_trading_critical",
            "freshness": freshness,
        }.items()
        if value is not None
    }
    return {"quote": compact}


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


def _execution_rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
    live = candidate.get("live_market_data")
    quote = live.get("quote") if isinstance(live, dict) else None
    quote = quote if isinstance(quote, dict) else {}
    bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
    has_two_way = (
        bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
    )
    freshness = quote.get("freshness")
    freshness = freshness if isinstance(freshness, dict) else {}
    bid_ask_age_ms = _number(freshness.get("bid_ask_age_ms"))
    is_fresh = (
        has_two_way
        and bid_ask_age_ms is not None
        and bid_ask_age_ms <= _EXECUTION_STALE_AFTER_MS
    )
    spread = _number(quote.get("spread_percent_from_live_prices"))
    turnover = _number(candidate.get("total_value_traded")) or 0.0
    return (
        0 if is_fresh else 1 if has_two_way else 2,
        spread if spread is not None else float("inf"),
        bid_ask_age_ms if bid_ask_age_ms is not None else float("inf"),
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


def _filter_option_display_values(response: Any, key: str) -> list[str]:
    if not hasattr(response, "model_dump"):
        return []
    payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)
    options = payload.get("filterOptions")
    if not isinstance(options, dict):
        return []
    entries = options.get(key)
    if not isinstance(entries, list):
        return []

    values: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        count = _number(entry.get("numberOfOrderbooks"))
        if count is not None and count <= 0:
            continue
        display = str(entry.get("displayName") or entry.get("value") or "").strip()
        if display:
            values.setdefault(display.casefold(), display)
    return [values[key] for key in sorted(values)]


def _display_value_key(value: str) -> str:
    return " ".join(value.casefold().replace("_", " ").split())


def _merge_display_values(*groups: list[str]) -> list[str]:
    values: dict[str, str] = {}
    for group in groups:
        for raw in group:
            value = str(raw).strip()
            if value:
                values.setdefault(_display_value_key(value), value)
    return [values[key] for key in sorted(values)]


def _available_filter_values(
    candidates: list[dict[str, Any]],
    *,
    upstream_issuers: list[str] | None = None,
    upstream_sub_types: list[str] | None = None,
) -> dict[str, Any]:
    leverage_values = [
        value
        for candidate in candidates
        if (value := _number(candidate.get("leverage"))) is not None
    ]
    return {
        "issuers": _merge_display_values(
            upstream_issuers or [],
            _display_values(candidates, "issuer"),
        ),
        "sub_types": _merge_display_values(
            upstream_sub_types or [],
            _display_values(candidates, "sub_type"),
        ),
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
    discovery_source: str = "avanza_filter_feed"
    execution_products: list[dict[str, Any]] | None = None
    execution_metadata: dict[str, Any] | None = None


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
            "discovery_source": snapshot.discovery_source,
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
            "Upstream-supported structural filters (issuer and warrant sub-type) may be pushed "
            "down before paging; all filters are still re-applied locally before ranking. "
            "pagination.total is the eligible filtered count and snapshot.scanned_count is the "
            "number of candidates actually scanned after structural pushdown. Exact issuer/sub-type "
            "vocabulary is supplemented from upstream filter metadata when available; leverage "
            "availability is derived from scanned candidates. Calls using snapshot_id reuse the "
            "frozen ranking and do not refetch market data. discovery_bid/discovery_ask and "
            "the ranking spread come from Avanza's filter feed, which live testing showed can lag "
            "authenticated trading-critical market-maker quotes materially; do not use discovery "
            "prices as execution evidence. When authenticated market-data delegation is available, "
            "a new snapshot also includes execution with a top-candidate shortlist re-ranked from "
            "authenticated bid/ask quality and bid/ask freshness. For leveraged market-maker "
            "products, last_trade is informational only and is not an execution-freshness signal. "
            "Initial quote collection is non-atomic because upstream pages are fetched over time; "
            "after the first page establishes the total, remaining pages may be fetched concurrently. "
            "If pagination.has_more is true, this response is only a partial view of the structural "
            "snapshot page."
        ),
    }


class LeveragedScreenService:
    def __init__(
        self,
        client: AvanzaClient,
        catalog: InstrumentCatalog | None = None,
    ) -> None:
        self._market = MarketDataService(client)
        self._catalog = catalog if catalog is not None else fresh_default_instrument_catalog()

    def _request_size(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        product_type: ProductType,
    ) -> int:
        if self._catalog is not None:
            try:
                count = self._catalog.count_by_underlying(
                    underlying_order_book_id,
                    direction=direction,
                    product_types=[product_type],
                )
            except (OSError, ValueError):
                count = 0
            if count > 0:
                return count
        return _FALLBACK_REQUEST_SIZE

    async def _collect_certificates(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        filters: ScreenFilters,
        _legacy_limit: int | None = None,
    ) -> dict[str, Any]:
        request_size = self._request_size(
            underlying_order_book_id, direction, "certificate"
        )

        async def fetch(offset: int):
            return await self._market.filter_certificates(
                _LeveragedCertificateFilterRequest(
                    filter=CertificateFilter(
                        directions=[direction],
                        issuers=list(filters.issuers),
                        underlyingInstruments=[underlying_order_book_id],
                    ),
                    offset=offset,
                    limit=request_size,
                    sortBy=SortBy(field="name", order="asc"),
                )
            )

        first = await fetch(0)
        items: list[Any] = list(first.certificates)
        total = first.totalNumberOfOrderbooks

        if total is not None and len(first.certificates) < total:
            # Avanza may accept the requested page size or cap it server-side.
            # Advance by the number of rows actually returned to avoid gaps.
            page_step = len(first.certificates)
            if page_step <= 0:
                return {
                    "products": [],
                    "upstream_total": total,
                    "scanned_count": 0,
                    "quote_complete_count": 0,
                    "available_issuers": _filter_option_display_values(first, "issuers"),
                    "available_sub_types": [],
                }
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

            async def fetch_bounded(offset: int):
                async with semaphore:
                    return await fetch(offset)

            offsets = list(range(page_step, total, page_step))
            if offsets:
                responses = await asyncio.gather(
                    *(fetch_bounded(offset) for offset in offsets)
                )
                for response in responses:
                    items.extend(response.certificates)
        else:
            page_step = len(first.certificates)
            offset = page_step
            while first.certificates and page_step > 0 and (total is None or offset < total):
                response = await fetch(offset)
                page = response.certificates
                if not page:
                    break
                items.extend(page)
                if response.totalNumberOfOrderbooks is not None:
                    total = response.totalNumberOfOrderbooks
                offset += len(page)
                if len(page) < page_step:
                    break
        products = [_normalize_candidate(item, "certificate") for item in items]
        return {
            "products": products,
            "upstream_total": total,
            "scanned_count": len(items),
            "quote_complete_count": sum(
                _has_two_way_quote(item) for item in products
            ),
            "available_issuers": _filter_option_display_values(first, "issuers"),
            "available_sub_types": [],
        }

    async def _collect_warrants(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        filters: ScreenFilters,
        _legacy_limit: int | None = None,
    ) -> dict[str, Any]:
        request_size = self._request_size(
            underlying_order_book_id, direction, "warrant"
        )

        async def fetch(offset: int):
            return await self._market.filter_warrants(
                _LeveragedWarrantFilterRequest(
                    filter=WarrantFilter(
                        directions=[direction],
                        subTypes=list(filters.sub_types),
                        issuers=list(filters.issuers),
                        underlyingInstruments=[underlying_order_book_id],
                    ),
                    offset=offset,
                    limit=request_size,
                    sortBy=SortBy(field="name", order="asc"),
                )
            )

        first = await fetch(0)
        items: list[Any] = list(first.warrants)
        total = first.totalNumberOfOrderbooks

        if total is not None and len(first.warrants) < total:
            # Avanza may accept the requested page size or cap it server-side.
            # Advance by the number of rows actually returned to avoid gaps.
            page_step = len(first.warrants)
            if page_step <= 0:
                return {
                    "products": [],
                    "upstream_total": total,
                    "scanned_count": 0,
                    "quote_complete_count": 0,
                    "available_issuers": _filter_option_display_values(first, "issuers"),
                    "available_sub_types": _filter_option_display_values(first, "subTypes"),
                }
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

            async def fetch_bounded(offset: int):
                async with semaphore:
                    return await fetch(offset)

            offsets = list(range(page_step, total, page_step))
            if offsets:
                responses = await asyncio.gather(
                    *(fetch_bounded(offset) for offset in offsets)
                )
                for response in responses:
                    items.extend(response.warrants)
        else:
            page_step = len(first.warrants)
            offset = page_step
            while first.warrants and page_step > 0 and (total is None or offset < total):
                response = await fetch(offset)
                page = response.warrants
                if not page:
                    break
                items.extend(page)
                if response.totalNumberOfOrderbooks is not None:
                    total = response.totalNumberOfOrderbooks
                offset += len(page)
                if len(page) < page_step:
                    break
        products = [_normalize_candidate(item, "warrant") for item in items]
        return {
            "products": products,
            "upstream_total": total,
            "scanned_count": len(items),
            "quote_complete_count": sum(
                _has_two_way_quote(item) for item in products
            ),
            "available_issuers": _filter_option_display_values(first, "issuers"),
            "available_sub_types": _filter_option_display_values(first, "subTypes"),
        }

    async def screen(
        self,
        underlying_order_book_id: str,
        direction: Direction,
        product_types: list[ProductType],
        page_size: int,
        filters: ScreenFilters | None = None,
        *,
        prefer_catalog: bool = True,
    ) -> dict[str, Any]:
        if not product_types or len(set(product_types)) != len(product_types):
            raise ValueError("product_types must contain unique product types")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        selected_filters = _normalize_filters(filters or ScreenFilters())
        _validate_filters(selected_filters)

        started_at, timer = datetime.now(timezone.utc), perf_counter()
        families: dict[str, dict[str, Any]] = {}
        all_products: list[dict[str, Any]] = []
        upstream_issuers: list[str] = []
        upstream_sub_types: list[str] = []
        scanned_count = quote_complete_count = 0
        discovery_source = "avanza_filter_feed"

        catalog_rows: list[dict[str, Any]] = []
        if prefer_catalog and self._catalog is not None and _catalog_can_satisfy_filters(selected_filters):
            try:
                catalog_count = self._catalog.count_by_underlying(
                    underlying_order_book_id,
                    direction=direction,
                    product_types=product_types,
                )
                if catalog_count > 0:
                    catalog_rows = self._catalog.find_by_underlying(
                        underlying_order_book_id,
                        direction=direction,
                        product_types=product_types,
                        limit=catalog_count,
                    )
            except (OSError, ValueError, AttributeError):
                catalog_rows = []

        if catalog_rows:
            discovery_source = "fresh_local_instrument_catalog"
            all_products = [_catalog_candidate(row) for row in catalog_rows]
            scanned_count = len(all_products)
            for product_type in product_types:
                family_products = [
                    item for item in all_products if item.get("product_type") == product_type
                ]
                families[product_type] = {
                    "upstream_total": len(family_products),
                    "scanned_count": len(family_products),
                    "quote_complete_count": 0,
                }
        else:
            collectors = {
                "certificate": self._collect_certificates,
                "warrant": self._collect_warrants,
            }
            results = await asyncio.gather(
                *(
                    collectors[product_type](
                        underlying_order_book_id,
                        direction,
                        selected_filters,
                    )
                    for product_type in product_types
                ),
                return_exceptions=True,
            )
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
                upstream_issuers.extend(result.get("available_issuers", []))
                upstream_sub_types.extend(result.get("available_sub_types", []))

        available_filter_values = _available_filter_values(
            all_products,
            upstream_issuers=upstream_issuers,
            upstream_sub_types=upstream_sub_types,
        )
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
        completed_at = datetime.now(timezone.utc)
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
            discovery_source,
        )
        _SNAPSHOTS.put(snapshot)
        return _page(snapshot, 0, page_size)

    async def enrich_snapshot(
        self,
        snapshot_id: str,
        underlying_order_book_id: str,
        direction: Direction,
        offset: int,
        page_size: int,
    ) -> dict[str, Any]:
        """Enrich and globally rank the complete snapshot, then page the result."""
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        snapshot = _SNAPSHOTS.get(snapshot_id)
        if (
            snapshot.underlying_order_book_id != underlying_order_book_id
            or snapshot.direction != direction
        ):
            raise ValueError(
                "snapshot_id does not match the supplied underlying_order_book_id and direction"
            )

        if snapshot.execution_products is None:
            started_at = datetime.now(timezone.utc)
            timer = perf_counter()
            selected_products = snapshot.products
            order_book_ids = [str(product["order_book_id"]) for product in selected_products]
            batch_error: str | None = None
            try:
                quotes = await self._market.get_authenticated_market_data_quotes(order_book_ids)
            except AvanzaAuthError:
                quotes = [None] * len(selected_products)
                batch_error = "auth_required"
            except AvanzaError:
                quotes = [None] * len(selected_products)
                batch_error = "upstream_unavailable"

            products: list[dict[str, Any]] = []
            for product, quote in zip(selected_products, quotes, strict=True):
                retrieved_at = datetime.now(timezone.utc).isoformat()
                if quote is None:
                    products.append(
                        {
                            **product,
                            "live_market_data_error": batch_error or "upstream_unavailable",
                            "live_market_data_retrieved_at": retrieved_at,
                        }
                    )
                    continue
                products.append(
                    {
                        **product,
                        "live_market_data": _compact_live_quote(quote),
                        "live_market_data_retrieved_at": retrieved_at,
                    }
                )

            products.sort(key=_execution_rank)
            completed_at = datetime.now(timezone.utc)
            authenticated_quote_count = sum(
                product.get("live_market_data", {}).get("quote", {}).get("source")
                == "authenticated_trading_critical"
                for product in products
            )
            live_two_way_count = sum(
                _has_two_way_quote(
                    {
                        "discovery_bid": product.get("live_market_data", {})
                        .get("quote", {})
                        .get("bid"),
                        "discovery_ask": product.get("live_market_data", {})
                        .get("quote", {})
                        .get("ask"),
                    }
                )
                for product in products
            )
            fresh_two_way_count = sum(
                bool(
                    product.get("live_market_data", {})
                    .get("quote", {})
                    .get("freshness", {})
                    .get("execution_is_fresh")
                )
                for product in products
            )
            snapshot.execution_products = products
            snapshot.execution_metadata = {
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_ms": round((perf_counter() - timer) * 1000, 3),
                "attempted_count": len(products),
                "enriched_count": sum("live_market_data" in product for product in products),
                "not_found_count": sum(
                    product.get("live_market_data_error") == "not_found" for product in products
                ),
                "upstream_error_count": sum(
                    product.get("live_market_data_error") == "upstream_unavailable"
                    for product in products
                ),
                "authenticated_quote_count": authenticated_quote_count,
                "two_way_quote_count": live_two_way_count,
                "fresh_two_way_quote_count": fresh_two_way_count,
                "auth_worker_calls": 1 if products else 0,
                "source": "authenticated_trading_critical_market_data_batch",
                "scope": "complete_snapshot",
                "cache_hit": False,
                "current_call_upstream_requests": len(products),
            }
        else:
            assert snapshot.execution_metadata is not None
            snapshot.execution_metadata = {**snapshot.execution_metadata, "cache_hit": True}

        products = snapshot.execution_products or []
        total = len(products)
        page = products[offset : offset + page_size]
        returned = len(page)
        has_more = offset + returned < total
        return {
            "snapshot_id": snapshot.snapshot_id,
            "underlying_order_book_id": snapshot.underlying_order_book_id,
            "direction": snapshot.direction,
            "structural_snapshot": {
                "eligible_count": len(snapshot.products),
                "expires_at": snapshot.expires_at.isoformat(),
                "ranking": _RANKING,
                "ranking_quote_source": (
                    "local_catalog"
                    if snapshot.discovery_source == "fresh_local_instrument_catalog"
                    else "delayed_filter_feed"
                ),
                "discovery_source": snapshot.discovery_source,
            },
            "enrichment": snapshot.execution_metadata,
            "pagination": {
                "total": total,
                "offset": offset,
                "page_size": page_size,
                "returned": returned,
                "has_more": has_more,
                "next_offset": offset + returned if has_more else None,
            },
            "products": page,
            "returned": returned,
            "ordering": "execution_ranking",
            "execution_ranking": _EXECUTION_RANKING,
            "execution_freshness_basis": "bid_ask_updated_at",
            "last_trade_role": "informational_only_for_leveraged_products",
            "data_note": (
                "The complete structural snapshot is authenticated before paging and globally "
                "re-ranked from live bid/ask quality and freshness. page_size only controls "
                "response pagination; it never limits the candidate universe."
            ),
        }

    async def enrich_page(
        self,
        snapshot_id: str,
        underlying_order_book_id: str,
        direction: Direction,
        offset: int,
        page_size: int,
    ) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")

        snapshot = _SNAPSHOTS.get(snapshot_id)
        if (
            snapshot.underlying_order_book_id != underlying_order_book_id
            or snapshot.direction != direction
        ):
            raise ValueError(
                "snapshot_id does not match the supplied underlying_order_book_id and direction"
            )

        selected_products = snapshot.products[offset : offset + page_size]
        started_at = datetime.now(timezone.utc)
        timer = perf_counter()
        order_book_ids = [
            str(product["order_book_id"]) for product in selected_products
        ]

        batch_error: str | None = None
        try:
            quotes = await self._market.get_authenticated_market_data_quotes(
                order_book_ids
            )
        except AvanzaAuthError:
            quotes = [None] * len(selected_products)
            batch_error = "auth_required"
        except AvanzaError:
            quotes = [None] * len(selected_products)
            batch_error = "upstream_unavailable"

        products: list[dict[str, Any]] = []
        for product, quote in zip(selected_products, quotes, strict=True):
            retrieved_at = datetime.now(timezone.utc).isoformat()
            if quote is None:
                products.append(
                    {
                        **product,
                        "live_market_data_error": (
                            batch_error or "upstream_unavailable"
                        ),
                        "live_market_data_retrieved_at": retrieved_at,
                    }
                )
                continue
            products.append(
                {
                    **product,
                    "live_market_data": _compact_live_quote(quote),
                    "live_market_data_retrieved_at": retrieved_at,
                }
            )

        completed_at = datetime.now(timezone.utc)
        returned = len(products)
        total = len(snapshot.products)
        has_more = offset + returned < total
        authenticated_quote_count = sum(
            product.get("live_market_data", {})
            .get("quote", {})
            .get("source")
            == "authenticated_trading_critical"
            for product in products
        )
        live_two_way_count = sum(
            _has_two_way_quote(
                {
                    "discovery_bid": product.get("live_market_data", {})
                    .get("quote", {})
                    .get("bid"),
                    "discovery_ask": product.get("live_market_data", {})
                    .get("quote", {})
                    .get("ask"),
                }
            )
            for product in products
        )
        fresh_two_way_count = sum(
            bool(
                product.get("live_market_data", {})
                .get("quote", {})
                .get("freshness", {})
                .get("execution_is_fresh")
            )
            for product in products
        )
        products.sort(key=_execution_rank)

        return {
            "snapshot_id": snapshot.snapshot_id,
            "underlying_order_book_id": snapshot.underlying_order_book_id,
            "direction": snapshot.direction,
            "structural_snapshot": {
                "eligible_count": total,
                "expires_at": snapshot.expires_at.isoformat(),
                "ranking": _RANKING,
                "ranking_quote_source": "delayed_filter_feed",
            },
            "enrichment": {
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_ms": round((perf_counter() - timer) * 1000, 3),
                "attempted_count": returned,
                "enriched_count": sum("live_market_data" in product for product in products),
                "not_found_count": sum(
                    product.get("live_market_data_error") == "not_found"
                    for product in products
                ),
                "upstream_error_count": sum(
                    product.get("live_market_data_error") == "upstream_unavailable"
                    for product in products
                ),
                "authenticated_quote_count": authenticated_quote_count,
                "two_way_quote_count": live_two_way_count,
                "fresh_two_way_quote_count": fresh_two_way_count,
                "auth_worker_calls": 1 if returned else 0,
                "source": "authenticated_trading_critical_market_data_batch",
                "scope": "requested_snapshot_page",
                "cache_hit": False,
                "current_call_upstream_requests": returned,
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
            "ordering": "execution_ranking",
            "execution_ranking": _EXECUTION_RANKING,
            "execution_freshness_basis": "bid_ask_updated_at",
            "last_trade_role": "informational_only_for_leveraged_products",
            "data_note": (
                "This internal refetch verifies the requested structural page through Avanza's "
                "authenticated trading-critical market-data endpoint, using one isolated auth "
                "worker and one session validation while issuing one governed upstream request "
                "per returned product. Products are returned in execution ranking based on fresh "
                "two-way bid/ask, spread, and bid/ask age. For leveraged market-maker products, "
                "last_trade is informational only. Large pages can take substantially longer."
            ),
        }

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
