"""Local SQLite discovery catalog for Avanza leveraged instruments.

The catalog is intentionally structural: it stores identity and relationship fields that
are useful for local discovery, but excludes bid/ask/spread/turnover and other values
that must remain live execution data.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from .client.base import AvanzaClient
from .client.exceptions import AvanzaRateLimitError
from .models.certificate import CertificateFilter, CertificateFilterRequest
from .models.filter import SortBy
from .models.warrant import WarrantFilter, WarrantFilterRequest
from .services.market_data_service import MarketDataService

ProductType = Literal["certificate", "warrant"]

_PAGE_SIZE = 100
_MAX_CONCURRENT_PAGES = 2
_RATE_LIMIT_ATTEMPTS = 4
_RATE_LIMIT_FALLBACK_SECONDS = 5
_MAX_RATE_LIMIT_DELAY_SECONDS = 60
_VALID_PRODUCT_TYPES = frozenset({"certificate", "warrant"})
_DEFAULT_CATALOG_MAX_AGE = timedelta(days=2)


def default_instrument_catalog_path() -> Path | None:
    """Return the default local catalog path when it can be resolved safely."""

    override = os.environ.get("AVANZA_MCP_INSTRUMENT_CATALOG")
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else None

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            root = Path(local_appdata)
            if root.is_absolute():
                return root / "avanza-mcp" / "instrument-catalog.sqlite3"
    return None


def fresh_default_instrument_catalog(
    *,
    max_age: timedelta = _DEFAULT_CATALOG_MAX_AGE,
) -> "InstrumentCatalog | None":
    """Return the default catalog only when a non-empty recent snapshot exists."""

    path = default_instrument_catalog_path()
    if path is None or not path.is_file():
        return None

    catalog = InstrumentCatalog(path)
    try:
        stats = catalog.stats()
        refreshed_at = stats.get("refreshed_at")
        if not refreshed_at or stats.get("row_count", 0) <= 0:
            return None
        refreshed = datetime.fromisoformat(str(refreshed_at))
        if refreshed.tzinfo is None:
            return None
        age = datetime.now(timezone.utc) - refreshed.astimezone(timezone.utc)
        if age > max_age:
            return None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    return catalog


@dataclass(frozen=True)
class CatalogInstrument:
    """Structural identity fields retained in the local discovery catalog."""

    product_type: ProductType
    order_book_id: str
    name: str
    direction: str
    issuer: str
    sub_type: str | None = None
    country_code: str | None = None
    marketplace_code: str | None = None
    underlying_order_book_id: str | None = None
    underlying_name: str | None = None
    underlying_instrument_type: str | None = None
    underlying_country_code: str | None = None
    leverage: float | None = None
    stop_loss: float | None = None


class InstrumentCatalog:
    """SQLite-backed local instrument discovery catalog."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> bool:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS instruments (
                product_type TEXT NOT NULL
                    CHECK (product_type IN ('certificate', 'warrant')),
                order_book_id TEXT NOT NULL,
                name TEXT NOT NULL,
                direction TEXT NOT NULL,
                issuer TEXT NOT NULL,
                sub_type TEXT,
                country_code TEXT,
                marketplace_code TEXT,
                underlying_order_book_id TEXT,
                underlying_name TEXT,
                underlying_instrument_type TEXT,
                underlying_country_code TEXT,
                leverage REAL,
                stop_loss REAL,
                PRIMARY KEY (product_type, order_book_id)
            );

            CREATE INDEX IF NOT EXISTS idx_instruments_underlying_direction
                ON instruments (underlying_order_book_id, direction, product_type);
            CREATE INDEX IF NOT EXISTS idx_instruments_name
                ON instruments (name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_instruments_issuer
                ON instruments (issuer COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_instruments_direction_type
                ON instruments (direction, product_type);
            """
        )
        existing_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(instruments)").fetchall()
        }
        for column, sql_type in (("leverage", "REAL"), ("stop_loss", "REAL")):
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE instruments ADD COLUMN {column} {sql_type}")
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS instrument_fts USING fts5(
                    product_type UNINDEXED,
                    order_book_id UNINDEXED,
                    name,
                    issuer,
                    underlying_name,
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
        except sqlite3.OperationalError as exc:
            if "fts5" not in str(exc).casefold():
                raise
            return False
        return True

    @staticmethod
    def _validate_rows(rows: list[CatalogInstrument]) -> None:
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if row.product_type not in _VALID_PRODUCT_TYPES:
                raise ValueError(f"Unsupported product type: {row.product_type}")
            if not row.order_book_id.isascii() or not row.order_book_id.isdecimal():
                raise ValueError("Catalog order_book_id must contain only ASCII numeric digits")
            if not row.name.strip():
                raise ValueError("Catalog instrument name must not be empty")
            key = (row.product_type, row.order_book_id)
            if key in seen:
                raise ValueError(f"Duplicate catalog instrument: {row.product_type}:{row.order_book_id}")
            seen.add(key)

    def replace_all(
        self,
        rows: Iterable[CatalogInstrument],
        *,
        refreshed_at: datetime,
    ) -> dict[str, Any]:
        """Atomically replace the current catalog after a complete upstream fetch."""

        normalized_rows = list(rows)
        self._validate_rows(normalized_rows)
        refreshed = refreshed_at.astimezone(timezone.utc).isoformat()

        certificate_count = sum(
            row.product_type == "certificate" for row in normalized_rows
        )
        warrant_count = sum(row.product_type == "warrant" for row in normalized_rows)

        connection = self._connect()
        try:
            fts5_enabled = self._create_schema(connection)
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM instruments")
            connection.executemany(
                """
                INSERT INTO instruments (
                    product_type,
                    order_book_id,
                    name,
                    direction,
                    issuer,
                    sub_type,
                    country_code,
                    marketplace_code,
                    underlying_order_book_id,
                    underlying_name,
                    underlying_instrument_type,
                    underlying_country_code,
                    leverage,
                    stop_loss
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row.product_type,
                        row.order_book_id,
                        row.name,
                        row.direction,
                        row.issuer,
                        row.sub_type,
                        row.country_code,
                        row.marketplace_code,
                        row.underlying_order_book_id,
                        row.underlying_name,
                        row.underlying_instrument_type,
                        row.underlying_country_code,
                        row.leverage,
                        row.stop_loss,
                    )
                    for row in normalized_rows
                ],
            )

            if fts5_enabled:
                connection.execute("DELETE FROM instrument_fts")
                connection.execute(
                    """
                    INSERT INTO instrument_fts (
                        product_type,
                        order_book_id,
                        name,
                        issuer,
                        underlying_name
                    )
                    SELECT
                        product_type,
                        order_book_id,
                        name,
                        issuer,
                        COALESCE(underlying_name, '')
                    FROM instruments
                    """
                )

            metadata = {
                "refreshed_at": refreshed,
                "row_count": str(len(normalized_rows)),
                "certificate_count": str(certificate_count),
                "warrant_count": str(warrant_count),
                "fts5_enabled": "1" if fts5_enabled else "0",
                "schema_version": "2",
            }
            connection.executemany(
                """
                INSERT INTO catalog_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                metadata.items(),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return {
            "path": str(self.path),
            "refreshed_at": refreshed,
            "row_count": len(normalized_rows),
            "certificate_count": certificate_count,
            "warrant_count": warrant_count,
            "fts5_enabled": fts5_enabled,
            "schema_version": 2,
        }

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys() if row[key] is not None}

    @staticmethod
    def _normalize_product_types(
        product_types: Iterable[ProductType] | None,
    ) -> tuple[str, ...]:
        if product_types is None:
            return ()
        values = tuple(dict.fromkeys(product_types))
        invalid = set(values) - _VALID_PRODUCT_TYPES
        if invalid:
            raise ValueError(f"Unsupported product type(s): {sorted(invalid)}")
        return values

    def find_by_order_book_id(self, order_book_id: str) -> list[dict[str, Any]]:
        """Return exact leveraged-instrument matches for one order-book ID."""

        if not order_book_id.isascii() or not order_book_id.isdecimal():
            raise ValueError("order_book_id must contain only ASCII numeric digits")

        connection = self._connect()
        try:
            self._create_schema(connection)
            rows = connection.execute(
                """
                SELECT *
                FROM instruments
                WHERE order_book_id = ?
                ORDER BY product_type
                """,
                (order_book_id,),
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def find_by_underlying(
        self,
        underlying_order_book_id: str,
        *,
        direction: str | None = None,
        product_types: Iterable[ProductType] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return locally indexed products for one underlying instrument."""

        if not underlying_order_book_id.isascii() or not underlying_order_book_id.isdecimal():
            raise ValueError("underlying_order_book_id must contain only ASCII numeric digits")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        selected_types = self._normalize_product_types(product_types)
        clauses = ["underlying_order_book_id = ?"]
        params: list[Any] = [underlying_order_book_id]
        if direction is not None:
            clauses.append("direction = ?")
            params.append(direction)
        if selected_types:
            placeholders = ",".join("?" for _ in selected_types)
            clauses.append(f"product_type IN ({placeholders})")
            params.extend(selected_types)
        params.append(limit)

        connection = self._connect()
        try:
            self._create_schema(connection)
            rows = connection.execute(
                f"""
                SELECT *
                FROM instruments
                WHERE {' AND '.join(clauses)}
                ORDER BY product_type, issuer COLLATE NOCASE, name COLLATE NOCASE
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def count_by_underlying(
        self,
        underlying_order_book_id: str,
        *,
        direction: str | None = None,
        product_types: Iterable[ProductType] | None = None,
    ) -> int:
        """Count locally indexed products for one underlying instrument."""

        if not underlying_order_book_id.isascii() or not underlying_order_book_id.isdecimal():
            raise ValueError("underlying_order_book_id must contain only ASCII numeric digits")

        selected_types = self._normalize_product_types(product_types)
        clauses = ["underlying_order_book_id = ?"]
        params: list[Any] = [underlying_order_book_id]
        if direction is not None:
            clauses.append("direction = ?")
            params.append(direction)
        if selected_types:
            placeholders = ",".join("?" for _ in selected_types)
            clauses.append(f"product_type IN ({placeholders})")
            params.extend(selected_types)

        connection = self._connect()
        try:
            self._create_schema(connection)
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM instruments
                WHERE {' AND '.join(clauses)}
                """,
                params,
            ).fetchone()
            return int(row["count"]) if row is not None else 0
        finally:
            connection.close()

    @staticmethod
    def _fts_query(value: str) -> str:
        tokens = re.findall(r"[\w.-]+", value, flags=re.UNICODE)
        if not tokens:
            return ""
        return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search(
        self,
        query: str,
        *,
        product_types: Iterable[ProductType] | None = None,
        direction: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search local identity metadata, using FTS5 when available."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        text = query.strip()
        if not text:
            return []

        selected_types = self._normalize_product_types(product_types)
        connection = self._connect()
        try:
            fts5_enabled = self._create_schema(connection)
            filters: list[str] = []
            params: list[Any] = []
            if direction is not None:
                filters.append("i.direction = ?")
                params.append(direction)
            if selected_types:
                placeholders = ",".join("?" for _ in selected_types)
                filters.append(f"i.product_type IN ({placeholders})")
                params.extend(selected_types)
            where_suffix = f" AND {' AND '.join(filters)}" if filters else ""

            if fts5_enabled:
                fts_query = self._fts_query(text)
                if fts_query:
                    try:
                        rows = connection.execute(
                            f"""
                            SELECT i.*
                            FROM instrument_fts AS f
                            JOIN instruments AS i
                              ON i.product_type = f.product_type
                             AND i.order_book_id = f.order_book_id
                            WHERE instrument_fts MATCH ?{where_suffix}
                            ORDER BY bm25(instrument_fts), i.name COLLATE NOCASE
                            LIMIT ?
                            """,
                            [fts_query, *params, limit],
                        ).fetchall()
                        if rows:
                            return [self._row_to_dict(row) for row in rows]
                    except sqlite3.OperationalError:
                        pass

            tokens = re.findall(r"[\w.-]+", text, flags=re.UNICODE) or [text]
            like_filters: list[str] = []
            like_params: list[Any] = []
            for token in tokens:
                like = f"%{token.casefold()}%"
                like_filters.append(
                    "(LOWER(i.name) LIKE ? OR LOWER(i.issuer) LIKE ? "
                    "OR LOWER(COALESCE(i.underlying_name, '')) LIKE ? "
                    "OR LOWER(i.order_book_id) LIKE ?)"
                )
                like_params.extend((like, like, like, like))
            if direction is not None:
                like_filters.append("i.direction = ?")
                like_params.append(direction)
            if selected_types:
                placeholders = ",".join("?" for _ in selected_types)
                like_filters.append(f"i.product_type IN ({placeholders})")
                like_params.extend(selected_types)
            like_params.append(limit)
            rows = connection.execute(
                f"""
                SELECT i.*
                FROM instruments AS i
                WHERE {' AND '.join(like_filters)}
                ORDER BY i.name COLLATE NOCASE
                LIMIT ?
                """,
                like_params,
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def stats(self) -> dict[str, Any]:
        """Return catalog metadata and current row counts."""

        connection = self._connect()
        try:
            self._create_schema(connection)
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM catalog_meta ORDER BY key"
                ).fetchall()
            }
            counts = {
                row["product_type"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT product_type, COUNT(*) AS count
                    FROM instruments
                    GROUP BY product_type
                    """
                ).fetchall()
            }
        finally:
            connection.close()

        return {
            "path": str(self.path),
            "refreshed_at": metadata.get("refreshed_at"),
            "row_count": sum(counts.values()),
            "certificate_count": counts.get("certificate", 0),
            "warrant_count": counts.get("warrant", 0),
            "fts5_enabled": metadata.get("fts5_enabled") == "1",
            "schema_version": int(metadata.get("schema_version", "1")),
        }


class InstrumentCatalogRefresher:
    """Download complete certificate/warrant lists before replacing the catalog."""

    def __init__(self, market: MarketDataService) -> None:
        self._market = market
        self._page_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

    async def _with_rate_limit_retry(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Retry one catalog page on explicit Avanza rate limiting."""

        for attempt in range(_RATE_LIMIT_ATTEMPTS):
            try:
                return await operation()
            except AvanzaRateLimitError as exc:
                if attempt + 1 >= _RATE_LIMIT_ATTEMPTS:
                    raise
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else _RATE_LIMIT_FALLBACK_SECONDS * (attempt + 1)
                )
                await asyncio.sleep(
                    max(1, min(delay, _MAX_RATE_LIMIT_DELAY_SECONDS))
                )
        raise AssertionError("unreachable rate-limit retry state")

    async def _certificate_page(self, offset: int):
        async with self._page_semaphore:
            async def request():
                return await self._market.filter_certificates(
                    CertificateFilterRequest(
                        filter=CertificateFilter(),
                        offset=offset,
                        limit=_PAGE_SIZE,
                        sortBy=SortBy(field="name", order="asc"),
                    )
                )

            return await self._with_rate_limit_retry(request)

    async def _warrant_page(self, offset: int):
        async with self._page_semaphore:
            async def request():
                return await self._market.filter_warrants(
                    WarrantFilterRequest(
                        filter=WarrantFilter(),
                        offset=offset,
                        limit=_PAGE_SIZE,
                        sortBy=SortBy(field="name", order="asc"),
                    )
                )

            return await self._with_rate_limit_retry(request)

    async def _collect_family(self, product_type: ProductType) -> tuple[list[Any], int | None]:
        fetch = self._certificate_page if product_type == "certificate" else self._warrant_page
        attr = "certificates" if product_type == "certificate" else "warrants"

        first = await fetch(0)
        first_items = list(getattr(first, attr))
        total = first.totalNumberOfOrderbooks
        items = list(first_items)

        if total is not None and len(first_items) == _PAGE_SIZE:
            offsets = list(range(_PAGE_SIZE, total, _PAGE_SIZE))
            responses = await asyncio.gather(*(fetch(offset) for offset in offsets))
            for response in responses:
                items.extend(getattr(response, attr))
        else:
            offset = len(first_items)
            page = first_items
            while page and (total is None or offset < total):
                response = await fetch(offset)
                page = list(getattr(response, attr))
                if not page:
                    break
                items.extend(page)
                if response.totalNumberOfOrderbooks is not None:
                    total = response.totalNumberOfOrderbooks
                offset += len(page)
                if len(page) < _PAGE_SIZE:
                    break

        if total is not None and len(items) != total:
            raise ValueError(
                f"Incomplete {product_type} catalog fetch: expected {total}, got {len(items)}"
            )
        return items, total

    @staticmethod
    def _normalize_item(item: Any, product_type: ProductType) -> CatalogInstrument:
        raw = item.model_dump(mode="json", by_alias=True, exclude_none=True)
        underlying = raw.get("underlyingInstrument")
        if not isinstance(underlying, dict):
            underlying = {}
        return CatalogInstrument(
            product_type=product_type,
            order_book_id=str(raw.get("orderbookId") or ""),
            name=str(raw.get("name") or ""),
            direction=str(raw.get("direction") or ""),
            issuer=str(raw.get("issuer") or ""),
            sub_type=(
                str(raw["subType"]) if raw.get("subType") is not None else None
            ),
            country_code=(
                str(raw["countryCode"]) if raw.get("countryCode") is not None else None
            ),
            marketplace_code=(
                str(raw["marketplaceCode"])
                if raw.get("marketplaceCode") is not None
                else None
            ),
            underlying_order_book_id=(
                str(underlying["orderbookId"])
                if underlying.get("orderbookId") is not None
                else None
            ),
            underlying_name=(
                str(underlying["name"]) if underlying.get("name") is not None else None
            ),
            underlying_instrument_type=(
                str(underlying["instrumentType"])
                if underlying.get("instrumentType") is not None
                else None
            ),
            underlying_country_code=(
                str(underlying["countryCode"])
                if underlying.get("countryCode") is not None
                else None
            ),
            leverage=(
                float(raw["leverage"]) if raw.get("leverage") is not None else None
            ),
            stop_loss=(
                float(raw["stopLoss"]) if raw.get("stopLoss") is not None else None
            ),
        )

    async def collect(self) -> tuple[list[CatalogInstrument], dict[str, Any]]:
        """Fetch both complete families without mutating the existing catalog."""

        certificate_result, warrant_result = await asyncio.gather(
            self._collect_family("certificate"),
            self._collect_family("warrant"),
        )
        certificate_items, certificate_total = certificate_result
        warrant_items, warrant_total = warrant_result

        rows = [
            *(self._normalize_item(item, "certificate") for item in certificate_items),
            *(self._normalize_item(item, "warrant") for item in warrant_items),
        ]
        if not rows:
            raise ValueError("Refusing to replace the instrument catalog with an empty snapshot")

        return rows, {
            "upstream_certificate_total": certificate_total,
            "upstream_warrant_total": warrant_total,
            "collected_count": len(rows),
        }

    async def refresh(self, catalog: InstrumentCatalog) -> dict[str, Any]:
        """Fetch a complete snapshot, then atomically replace the local catalog."""

        rows, upstream = await self.collect()
        refreshed_at = datetime.now(timezone.utc)
        result = catalog.replace_all(rows, refreshed_at=refreshed_at)
        return {**result, **upstream}


async def refresh_instrument_catalog(path: str | Path) -> dict[str, Any]:
    """Refresh a catalog using a short-lived public Avanza HTTP client."""

    async with AvanzaClient(max_connections=8, max_keepalive_connections=8) as client:
        market = MarketDataService(client)
        return await InstrumentCatalogRefresher(market).refresh(InstrumentCatalog(path))


def stats_json(stats: dict[str, Any]) -> str:
    """Stable compact JSON for logs and manual refresh output."""

    return json.dumps(stats, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
