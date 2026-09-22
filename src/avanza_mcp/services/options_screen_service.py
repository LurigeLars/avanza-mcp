"""Snapshot-backed structural option-chain discovery and pagination."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from ..client.base import AvanzaClient
from ..models.filter import SortBy
from ..models.future_forward import FutureForwardMatrixFilter, FutureForwardMatrixRequest
from .market_data_service import MarketDataService

CallIndicator = Literal["CALL", "PUT"]
_MATRIX_PAGE_SIZE = 100
_MAX_CONCURRENT_EXPIRIES = 4
_SNAPSHOT_TTL = timedelta(minutes=10)
_ORDERING = "expiry_date_asc, option_type_asc, strike_price_asc, call_indicator_asc, name_asc"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dump(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(response, dict):
        return response
    raise TypeError("unexpected option matrix response")


def _positive_filter_values(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        count = item.get("numberOfOrderbooks")
        if value and (count is None or (_number(count) or 0) > 0):
            values.append(str(value))
    return values


def _expiry_values(filter_options: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for group in filter_options.get("endDates") or []:
        if not isinstance(group, dict):
            continue
        for child in group.get("children") or []:
            if not isinstance(child, dict):
                continue
            value = child.get("value")
            count = _number(child.get("numberOfOrderbooks"))
            if value and (count is None or count > 0):
                values.append(str(value))
    return sorted(set(values))


def _flatten_matched_options(
    rows: Any,
    *,
    option_type: str,
    expiry_date: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    contracts: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, indicator in (("call", "CALL"), ("put", "PUT")):
            side = row.get(key)
            if not isinstance(side, dict):
                continue
            order_book_id = side.get("orderbookId")
            if order_book_id is None:
                continue
            contracts.append(
                {
                    field: value
                    for field, value in {
                        "order_book_id": str(order_book_id),
                        "name": side.get("name"),
                        "country_code": side.get("countryCode"),
                        "call_indicator": indicator,
                        "call_indicator_label": side.get("callIndicator"),
                        "strike_price": _number(side.get("strikePrice")),
                        "expiry_date": expiry_date,
                        "option_type": option_type,
                    }.items()
                    if value is not None
                }
            )
    return contracts


@dataclass(frozen=True)
class OptionScreenSpec:
    option_types: tuple[str, ...] = ()
    call_indicators: tuple[CallIndicator, ...] = ()
    end_dates: tuple[str, ...] = ()
    min_strike: float | None = None
    max_strike: float | None = None


def _normalize_spec(spec: OptionScreenSpec) -> OptionScreenSpec:
    return OptionScreenSpec(
        option_types=tuple(
            sorted({value.strip().upper() for value in spec.option_types if value.strip()})
        ),
        call_indicators=tuple(sorted(set(spec.call_indicators))),
        end_dates=tuple(
            sorted({date.fromisoformat(value).isoformat() for value in spec.end_dates})
        ),
        min_strike=spec.min_strike,
        max_strike=spec.max_strike,
    )


def _validate_spec(spec: OptionScreenSpec) -> None:
    if spec.min_strike is not None and spec.min_strike < 0:
        raise ValueError("min_strike must be >= 0")
    if spec.max_strike is not None and spec.max_strike < 0:
        raise ValueError("max_strike must be >= 0")
    if (
        spec.min_strike is not None
        and spec.max_strike is not None
        and spec.min_strike > spec.max_strike
    ):
        raise ValueError("min_strike must be <= max_strike")


def _matches(contract: dict[str, Any], spec: OptionScreenSpec) -> bool:
    if spec.call_indicators and contract.get("call_indicator") not in spec.call_indicators:
        return False
    strike = _number(contract.get("strike_price"))
    if spec.min_strike is not None and (strike is None or strike < spec.min_strike):
        return False
    if spec.max_strike is not None and (strike is None or strike > spec.max_strike):
        return False
    return True


@dataclass(frozen=True)
class _OptionSnapshot:
    snapshot_id: str
    underlying_order_book_id: str
    spec: OptionScreenSpec
    scanned_option_types: tuple[str, ...]
    scanned_end_dates: tuple[str, ...]
    available_filter_values: dict[str, Any]
    underlying: dict[str, Any] | None
    contracts: list[dict[str, Any]]
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    scanned_pair_rows: int
    scanned_contract_count: int
    expires_at: datetime


class _SnapshotStore:
    def __init__(self) -> None:
        self._data: dict[str, _OptionSnapshot] = {}
        self._lock = Lock()

    def put(self, snapshot: _OptionSnapshot) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._data = {
                key: value for key, value in self._data.items() if value.expires_at > now
            }
            self._data[snapshot.snapshot_id] = snapshot

    def get(self, snapshot_id: str) -> _OptionSnapshot:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._data = {
                key: value for key, value in self._data.items() if value.expires_at > now
            }
            snapshot = self._data.get(snapshot_id)
        if snapshot is None:
            raise ValueError(
                "snapshot_id was not found or has expired; create a new option snapshot"
            )
        return snapshot


_SNAPSHOTS = _SnapshotStore()


def _page(snapshot: _OptionSnapshot, offset: int, page_size: int) -> dict[str, Any]:
    total = len(snapshot.contracts)
    options = snapshot.contracts[offset : offset + page_size]
    returned = len(options)
    has_more = offset + returned < total
    return {
        "snapshot_id": snapshot.snapshot_id,
        "underlying_order_book_id": snapshot.underlying_order_book_id,
        "underlying": snapshot.underlying,
        "filters": {
            "option_types": list(snapshot.spec.option_types),
            "call_indicators": list(snapshot.spec.call_indicators),
            "end_dates": list(snapshot.spec.end_dates),
            **(
                {"min_strike": snapshot.spec.min_strike}
                if snapshot.spec.min_strike is not None
                else {}
            ),
            **(
                {"max_strike": snapshot.spec.max_strike}
                if snapshot.spec.max_strike is not None
                else {}
            ),
        },
        "available_filter_values": snapshot.available_filter_values,
        "snapshot": {
            "started_at": snapshot.started_at.isoformat(),
            "completed_at": snapshot.completed_at.isoformat(),
            "duration_ms": snapshot.duration_ms,
            "scanned_option_types": list(snapshot.scanned_option_types),
            "scanned_end_dates": list(snapshot.scanned_end_dates),
            "scanned_pair_rows": snapshot.scanned_pair_rows,
            "scanned_contract_count": snapshot.scanned_contract_count,
            "eligible_count": total,
            "atomic": False,
            "comparison_complete": True,
            "market_data_enriched": False,
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
        "options": options,
        "returned": returned,
        "ordering": _ORDERING,
        "data_note": (
            "Structural option-chain snapshot only. Every selected option type and expiry is "
            "fully paged before local CALL/PUT and strike filtering. Contract rows contain "
            "identity, expiry and strike but are not enriched with live bid/ask, spread, Greeks "
            "or turnover. Calls using snapshot_id reuse the frozen chain and do not refetch Avanza."
        ),
    }


class OptionsScreenService:
    def __init__(self, client: AvanzaClient) -> None:
        self._market = MarketDataService(client)

    async def _matrix(
        self,
        underlying_order_book_id: str,
        *,
        option_type: str | None = None,
        expiry_date: str | None = None,
        offset: int = 0,
        limit: int = 1,
    ) -> dict[str, Any]:
        request = FutureForwardMatrixRequest(
            filter=FutureForwardMatrixFilter(
                underlyingInstruments=[underlying_order_book_id],
                optionTypes=[option_type] if option_type else [],
                endDates=[expiry_date] if expiry_date else [],
                callIndicators=[],
            ),
            offset=offset,
            limit=limit,
            sortBy=SortBy(field="strikePrice", order="asc"),
        )
        return _dump(await self._market.list_futures_forwards(request))

    async def _discover(
        self,
        underlying_order_book_id: str,
        requested_option_types: tuple[str, ...],
    ) -> tuple[
        tuple[str, ...],
        dict[str, tuple[str, ...]],
        dict[str, Any],
        dict[str, Any] | None,
    ]:
        base = await self._matrix(underlying_order_book_id)
        filter_options = (
            base.get("filterOptions")
            if isinstance(base.get("filterOptions"), dict)
            else {}
        )
        available_option_types = tuple(
            sorted(set(_positive_filter_values(filter_options.get("optionTypes"))))
        )
        selected_types = requested_option_types or available_option_types

        type_responses = await asyncio.gather(
            *(
                self._matrix(
                    underlying_order_book_id,
                    option_type=option_type,
                )
                for option_type in selected_types
            )
        )
        expiries_by_type: dict[str, tuple[str, ...]] = {}
        all_end_dates: set[str] = set()
        for option_type, response in zip(selected_types, type_responses, strict=True):
            scoped = (
                response.get("filterOptions")
                if isinstance(response.get("filterOptions"), dict)
                else {}
            )
            expiries = tuple(_expiry_values(scoped))
            expiries_by_type[option_type] = expiries
            all_end_dates.update(expiries)

        available = {
            "option_types": list(available_option_types),
            "call_indicators": (
                sorted(set(_positive_filter_values(filter_options.get("callIndicators"))))
                or ["CALL", "PUT"]
            ),
            "end_dates": sorted(all_end_dates),
        }
        underlying = (
            base.get("underlyingInstrument")
            if isinstance(base.get("underlyingInstrument"), dict)
            else None
        )
        return selected_types, expiries_by_type, available, underlying

    async def _scan_expiry(
        self,
        underlying_order_book_id: str,
        option_type: str,
        expiry_date: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[dict[str, Any]], int]:
        contracts: list[dict[str, Any]] = []
        offset = 0
        rows_scanned = 0
        async with semaphore:
            while True:
                response = await self._matrix(
                    underlying_order_book_id,
                    option_type=option_type,
                    expiry_date=expiry_date,
                    offset=offset,
                    limit=_MATRIX_PAGE_SIZE,
                )
                rows = response.get("matchedOptions")
                if not isinstance(rows, list):
                    rows = []
                rows_scanned += len(rows)
                contracts.extend(
                    _flatten_matched_options(
                        rows,
                        option_type=option_type,
                        expiry_date=expiry_date,
                    )
                )
                offset += len(rows)
                total = int(_number(response.get("totalNumberOfOrderbooks")) or 0)
                if (
                    not rows
                    or len(rows) < _MATRIX_PAGE_SIZE
                    or (total and offset >= total)
                ):
                    break
        return contracts, rows_scanned

    async def screen(
        self,
        underlying_order_book_id: str,
        page_size: int,
        spec: OptionScreenSpec | None = None,
    ) -> dict[str, Any]:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        selected_spec = _normalize_spec(spec or OptionScreenSpec())
        _validate_spec(selected_spec)

        started_at = datetime.now(timezone.utc)
        timer = perf_counter()
        selected_types, expiries_by_type, available, underlying = await self._discover(
            underlying_order_book_id,
            selected_spec.option_types,
        )

        requested_dates = set(selected_spec.end_dates)
        scan_pairs: list[tuple[str, str]] = []
        for option_type in selected_types:
            expiries = expiries_by_type.get(option_type, ())
            chosen = [
                value
                for value in expiries
                if not requested_dates or value in requested_dates
            ]
            scan_pairs.extend((option_type, value) for value in chosen)

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_EXPIRIES)
        results = await asyncio.gather(
            *(
                self._scan_expiry(
                    underlying_order_book_id,
                    option_type,
                    expiry_date,
                    semaphore,
                )
                for option_type, expiry_date in scan_pairs
            )
        )

        all_contracts: list[dict[str, Any]] = []
        rows_scanned = 0
        for contracts, pair_rows in results:
            all_contracts.extend(contracts)
            rows_scanned += pair_rows

        deduped: dict[str, dict[str, Any]] = {}
        for contract in all_contracts:
            deduped.setdefault(contract["order_book_id"], contract)
        scanned_contracts = list(deduped.values())

        eligible = [
            contract
            for contract in scanned_contracts
            if _matches(contract, selected_spec)
        ]
        eligible.sort(
            key=lambda contract: (
                str(contract.get("expiry_date") or ""),
                str(contract.get("option_type") or ""),
                (
                    _number(contract.get("strike_price"))
                    if _number(contract.get("strike_price")) is not None
                    else float("inf")
                ),
                str(contract.get("call_indicator") or ""),
                str(contract.get("name") or ""),
            )
        )

        strike_values = [
            value
            for contract in scanned_contracts
            if (value := _number(contract.get("strike_price"))) is not None
        ]
        available = {
            **available,
            "strike": {
                "reported_count": len(strike_values),
                "min": min(strike_values) if strike_values else None,
                "max": max(strike_values) if strike_values else None,
            },
        }

        completed_at = datetime.now(timezone.utc)
        stored_spec = OptionScreenSpec(
            option_types=tuple(selected_types),
            call_indicators=selected_spec.call_indicators,
            end_dates=selected_spec.end_dates,
            min_strike=selected_spec.min_strike,
            max_strike=selected_spec.max_strike,
        )
        snapshot = _OptionSnapshot(
            uuid4().hex,
            underlying_order_book_id,
            stored_spec,
            tuple(selected_types),
            tuple(sorted({expiry for _, expiry in scan_pairs})),
            available,
            underlying,
            eligible,
            started_at,
            completed_at,
            round((perf_counter() - timer) * 1000, 3),
            rows_scanned,
            len(scanned_contracts),
            completed_at + _SNAPSHOT_TTL,
        )
        _SNAPSHOTS.put(snapshot)
        return _page(snapshot, 0, page_size)

    def get_page(
        self,
        snapshot_id: str,
        underlying_order_book_id: str,
        offset: int,
        page_size: int,
        spec: OptionScreenSpec | None = None,
    ) -> dict[str, Any]:
        if offset < 0 or page_size < 1:
            raise ValueError("offset must be >= 0 and page_size must be >= 1")
        snapshot = _SNAPSHOTS.get(snapshot_id)
        if snapshot.underlying_order_book_id != underlying_order_book_id:
            raise ValueError(
                "snapshot_id does not match the supplied underlying_order_book_id"
            )
        if spec is not None:
            normalized = _normalize_spec(spec)
            _validate_spec(normalized)
            expected = snapshot.spec
            if normalized.option_types and normalized.option_types != expected.option_types:
                raise ValueError("option_types do not match the stored snapshot")
            if (
                normalized.call_indicators
                and normalized.call_indicators != expected.call_indicators
            ):
                raise ValueError("call_indicators do not match the stored snapshot")
            if normalized.end_dates and normalized.end_dates != expected.end_dates:
                raise ValueError("end_dates do not match the stored snapshot")
            if (
                normalized.min_strike is not None
                and normalized.min_strike != expected.min_strike
            ):
                raise ValueError("min_strike does not match the stored snapshot")
            if (
                normalized.max_strike is not None
                and normalized.max_strike != expected.max_strike
            ):
                raise ValueError("max_strike does not match the stored snapshot")
        return _page(snapshot, offset, page_size)
