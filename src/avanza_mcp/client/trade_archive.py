"""Private, incremental SQLite evidence archive for Avanza trades and notes.

This is NOT the canonical Trade Spine position/event ledger. It retains the
broker's transaction evidence, plus PDF fetch state, for later reconciliation.

Only the isolated BankID worker can access note references or PDF bytes.
Neither secrets nor filesystem paths are returned to MCP clients.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..models.account import ArchiveSync, ArchivedTrades, ArchivedTransaction, Money
from .accounts import AccountAuthExpired, AccountReadError
from .contract_notes import (
    _MAX_PDF_BYTES,
    _MAX_EXPORT_BYTES,
    _SAFE_ID,
    _note_directory,
    _read_pdf,
    _write_pdf,
)

if TYPE_CHECKING:
    from .accounts import AccountClient

_STOCKHOLM = ZoneInfo("Europe/Stockholm")
_MAX_DATE_SPAN_DAYS = 366
_MAX_SCAN_CALLS = 128
_MAX_TRANSACTION_PAGE = 1000
_SYNC_LOOKBACK_DAYS = 6  # Seven calendar days including the last scanned day
_MAX_NOTES_PER_SYNC = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_transaction (
    account_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    traded_at TEXT NOT NULL,
    trade_day TEXT NOT NULL,
    settlement_date TEXT,
    description TEXT NOT NULL,
    instrument_name TEXT,
    isin TEXT,
    order_book_id TEXT,
    volume TEXT,
    amount TEXT,
    amount_currency TEXT,
    commission TEXT,
    commission_currency TEXT,
    note_owner_ref TEXT,
    note_id TEXT,
    note_file TEXT,
    note_sha256 TEXT,
    note_attempted_at TEXT,
    note_retry_after TEXT,
    note_error TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(account_id, transaction_id)
);
CREATE INDEX IF NOT EXISTS transaction_by_trade_day
    ON broker_transaction(trade_day, transaction_id);
CREATE INDEX IF NOT EXISTS transaction_missing_note
    ON broker_transaction(trade_day, note_retry_after)
    WHERE note_file IS NULL;
CREATE TABLE IF NOT EXISTS archive_meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_coverage (
    first_day TEXT NOT NULL,
    last_day TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY(first_day, last_day),
    CHECK(first_day <= last_day)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _local_today() -> date:
    return datetime.now(_STOCKHOLM).date()


def _archive_location() -> tuple[Path, Path, str]:
    """Fixed user-local archive; no caller can specify filesystem paths."""
    folder, hint = _note_directory()
    database = folder.parent / "trade_archive.sqlite3"
    if database.is_symlink():
        raise AccountReadError("unsafe_storage")
    return database, folder, hint


def _open_archive(*, create: bool) -> tuple[sqlite3.Connection | None, Path, str]:
    database, folder, hint = _archive_location()
    if not create and not database.exists():
        return None, folder, hint
    if database.is_symlink():
        raise AccountReadError("unsafe_storage")
    if database.exists() and not database.is_file():
        raise AccountReadError("unsafe_storage")
    try:
        # Cache-only reads open the existing SQLite database in true read-only
        # mode. They cannot initialize/migrate tables or mutate WAL state.
        connection = sqlite3.connect(
            database if create else database.as_uri() + "?mode=ro",
            timeout=10.0, uri=not create,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if create:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise AccountReadError("unsupported_archive_version")
            connection.executescript(_SCHEMA)
            if version == 0:
                connection.execute("PRAGMA user_version=1")
            if os.name != "nt":
                os.chmod(database, 0o600)
        else:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise AccountReadError("unsupported_archive_version")
        return connection, folder, hint
    except sqlite3.Error:
        raise AccountReadError("archive_unavailable") from None


def _close(connection: sqlite3.Connection | None) -> None:
    if connection is not None:
        connection.close()


def _meta(connection: sqlite3.Connection, key: str) -> str | None:
    record = connection.execute(
        "SELECT value FROM archive_meta WHERE name = ?", (key,)
    ).fetchone()
    return record["value"] if record is not None else None


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """INSERT INTO archive_meta(name,value) VALUES (?,?)
           ON CONFLICT(name) DO UPDATE SET value=excluded.value""",
        (key, value),
    )


def _broker_date(row: dict[str, Any]) -> date:
    raw = row.get("date")
    if not isinstance(raw, str) or len(raw) < 10:
        raise AccountReadError("invalid_transaction_date")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        raise AccountReadError("invalid_transaction_date") from None


async def _fetch_transactions(
    account: AccountClient, first: date, last: date
) -> tuple[list[dict[str, Any]], int, int]:
    """Date-bisected, complete response; no guessed page-offset API.

    When a *single day* itself has >1000 rows, fail closed rather than claim
    complete coverage. Intra-day vs settled records are intentionally preserved
    as distinct Avanza transaction IDs; Trade Spine owns reconciliation.
    """
    request_count = 0

    async def request(start: date, end: date) -> list[dict[str, Any]]:
        nonlocal request_count
        request_count += 1
        if request_count > _MAX_SCAN_CALLS:
            raise AccountReadError("archive_scan_limit")
        payload = await account._get(
            "/_api/transactions/list",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "maxElements": _MAX_TRANSACTION_PAGE,
                "transactionTypes": "BUY,SELL",
            },
        )
        rows = payload.get("transactions")
        total = payload.get("transactionsAfterFiltering")
        if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
            raise AccountReadError("invalid_transaction_list")
        if total > _MAX_TRANSACTION_PAGE:
            if start == end:
                raise AccountReadError("single_day_exceeds_1000")
            midpoint = start + timedelta(days=(end - start).days // 2)
            return (await request(start, midpoint)) + (
                await request(midpoint + timedelta(days=1), end)
            )
        if total != len(rows) or not all(isinstance(item, dict) for item in rows):
            raise AccountReadError("incomplete_transaction_list")
        return rows

    payload = await request(first, last)
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in payload:
        account_data = row.get("account")
        account_id = account_data.get("id") if isinstance(account_data, dict) else None
        transaction_id = row.get("id")
        if (
            not isinstance(account_id, (str, int))
            or isinstance(account_id, bool)
            or not isinstance(transaction_id, (str, int))
            or isinstance(transaction_id, bool)
        ):
            raise AccountReadError("invalid_transaction_identity")
        key = str(account_id), str(transaction_id)
        day = _broker_date(row)
        if not first <= day <= last or row.get("type") not in {"BUY", "SELL"}:
            raise AccountReadError("unexpected_transaction")
        if key in unique and unique[key] != row:
            raise AccountReadError("conflicting_transaction_duplicate")
        unique[key] = row
    rows = sorted(unique.values(), key=lambda r: (_broker_date(r), str(r["id"])))
    return rows, request_count, len(payload) - len(rows)


def _optional_ref(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and _SAFE_ID.fullmatch(raw) else None


def _money(model: Money | None) -> tuple[str | None, str | None]:
    return ((model.amount, model.currency) if model is not None else (None, None))


def _upsert_rows(
    connection: sqlite3.Connection, account: AccountClient, rows: list[dict[str, Any]], now: str
) -> tuple[int, int]:
    inserted = updated = 0
    for raw in rows:
        projected = account._transaction(raw)
        owner = raw.get("account")
        note_owner_ref = _optional_ref(
            owner.get("urlParameterId") if isinstance(owner, dict) else None
        )
        note_id = _optional_ref(raw.get("noteId"))
        amount, currency = _money(projected.amount)
        commission, commission_currency = _money(projected.commission)
        identity = (projected.account_id, projected.transaction_id)
        present = connection.execute(
            """SELECT 1 FROM broker_transaction
               WHERE account_id=? AND transaction_id=?""",
            identity,
        ).fetchone()
        if present is None:
            inserted += 1
        else:
            updated += 1
        connection.execute(
            """INSERT INTO broker_transaction (
                 account_id, transaction_id, side, traded_at, trade_day,
                 settlement_date, description, instrument_name, isin,
                 order_book_id, volume, amount, amount_currency,
                 commission, commission_currency, note_owner_ref, note_id,
                 first_seen_at, last_seen_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id,transaction_id) DO UPDATE SET
                 side=excluded.side,
                 traded_at=excluded.traded_at,
                 trade_day=excluded.trade_day,
                 settlement_date=excluded.settlement_date,
                 description=excluded.description,
                 instrument_name=excluded.instrument_name,
                 isin=excluded.isin,
                 order_book_id=excluded.order_book_id,
                 volume=excluded.volume,
                 amount=excluded.amount,
                 amount_currency=excluded.amount_currency,
                 commission=excluded.commission,
                 commission_currency=excluded.commission_currency,
                 note_owner_ref=COALESCE(excluded.note_owner_ref, broker_transaction.note_owner_ref),
                 note_id=COALESCE(excluded.note_id, broker_transaction.note_id),
                 last_seen_at=excluded.last_seen_at""",
            (
                *identity,
                projected.transaction_type,
                projected.date,
                projected.date[:10],
                projected.settlement_date,
                projected.description,
                projected.instrument_name,
                projected.isin,
                projected.order_book_id,
                projected.volume,
                amount,
                currency,
                commission,
                commission_currency,
                note_owner_ref,
                note_id,
                now,
                now,
            ),
        )
    return inserted, updated


def _valid_existing_pdf(file: Path) -> bool:
    if file.is_symlink() or not file.is_file():
        return False
    try:
        if not 0 < file.stat().st_size <= _MAX_PDF_BYTES:
            return False
        with file.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return False
            stream.seek(max(0, file.stat().st_size - 2048))
            return b"%%EOF" in stream.read()
    except OSError:
        return False


def _note_filename(traded_on: str, owner: str, note: str) -> str:
    fingerprint = hashlib.sha256(f"{owner}:{note}".encode("ascii")).hexdigest()[:24]
    return f"{traded_on.replace('-', '')}_{fingerprint}.pdf"


def _update_note(
    connection: sqlite3.Connection, owner: str, note: str,
    *, file_name: str | None, digest: str | None,
    attempted_at: str, retry_after: str | None, error_code: str | None,
) -> None:
    connection.execute(
        """UPDATE broker_transaction
           SET note_file=?, note_sha256=?, note_attempted_at=?,
               note_retry_after=?, note_error=?
           WHERE note_owner_ref=? AND note_id=?""",
        (file_name, digest, attempted_at, retry_after, error_code, owner, note),
    )


def _tomorrow_retry(now: datetime) -> str:
    # 01:00 the next calendar day in Stockholm; offset/DST handled by zoneinfo.
    local = now.astimezone(_STOCKHOLM)
    next_day = local.date() + timedelta(days=1)
    return datetime(
        next_day.year, next_day.month, next_day.day, 1, 0,
        tzinfo=_STOCKHOLM,
    ).astimezone(timezone.utc).isoformat()


async def _download_due_notes(
    account: AccountClient, connection: sqlite3.Connection,
    folder: Path, today: date, now: datetime, max_notes: int,
) -> tuple[int, int, int, int, int]:
    timestamp = now.isoformat()
    rows = connection.execute(
        """SELECT account_id, transaction_id, trade_day,
                  note_owner_ref, note_id, note_file, note_retry_after
           FROM broker_transaction
           WHERE trade_day < ?
             AND note_owner_ref IS NOT NULL AND note_id IS NOT NULL
           ORDER BY trade_day DESC, transaction_id""",
        (today.isoformat(),),
    ).fetchall()
    attempted = downloaded = reused = failed = deferred = 0
    saved_bytes = 0
    seen: set[tuple[str, str]] = set()

    for item in rows:
        owner, note = item["note_owner_ref"], item["note_id"]
        key = (owner, note)
        if key in seen:
            continue
        seen.add(key)
        previous = item["note_file"]
        if previous and _valid_existing_pdf(folder / previous):
            # Verified cached document; never download it again.
            reused += 1
            continue
        if item["note_retry_after"] and item["note_retry_after"] > timestamp:
            deferred += 1
            continue
        if attempted >= max_notes or saved_bytes >= _MAX_EXPORT_BYTES:
            deferred += 1
            continue

        attempted += 1
        filename = _note_filename(item["trade_day"], owner, note)
        target = folder / filename
        if _valid_existing_pdf(target):
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            with connection:
                _update_note(
                    connection, owner, note, file_name=filename, digest=digest,
                    attempted_at=timestamp, retry_after=None, error_code=None,
                )
            reused += 1
            continue

        try:
            document = await _read_pdf(account, owner, note)
            if saved_bytes + len(document) > _MAX_EXPORT_BYTES:
                raise AccountReadError("export_size_limit")
            _write_pdf(folder, filename, document)
            with connection:
                _update_note(
                    connection, owner, note, file_name=filename,
                    digest=hashlib.sha256(document).hexdigest(),
                    attempted_at=timestamp, retry_after=None, error_code=None,
                )
            downloaded += 1
            saved_bytes += len(document)
        except AccountAuthExpired:
            raise
        except (AccountReadError, OSError) as exc:
            code = str(exc) if isinstance(exc, AccountReadError) else "storage_error"
            if code not in {
                "http_404", "http_403", "http_429", "http_500",
                "invalid_pdf", "invalid_pdf_size", "invalid_pdf_type",
                "network", "export_size_limit", "storage_error",
                "request_not_allowed", "unsafe_storage", "unsafe_existing_file",
                "redirect_missing_location", "redirect_login",
                "redirect_insecure", "redirect_invalid_location",
                "redirect_same_origin", "redirect_avanza_subdomain",
                "redirect_external", "redirect_relative",
                "redirect_limit", "redirect_loop", "redirect_unsupported_status",
            }:
                code = "note_fetch_failed"
            with connection:
                _update_note(
                    connection, owner, note, file_name=None, digest=None,
                    attempted_at=timestamp, retry_after=_tomorrow_retry(now),
                    error_code=code,
                )
            failed += 1

    return attempted, downloaded, reused, failed, deferred


def _count_notes(connection: sqlite3.Connection, today: date) -> tuple[int, int, int, int]:
    same_day = connection.execute(
        "SELECT COUNT(*) FROM broker_transaction WHERE trade_day >= ? AND note_file IS NULL",
        (today.isoformat(),),
    ).fetchone()[0]
    no_ref = connection.execute(
        """SELECT COUNT(*) FROM broker_transaction
           WHERE trade_day < ? AND note_file IS NULL
             AND transaction_id NOT LIKE 'intra-day-%'
             AND (note_owner_ref IS NULL OR note_id IS NULL)""",
        (today.isoformat(),),
    ).fetchone()[0]
    due = connection.execute(
        """SELECT COUNT(*) FROM broker_transaction
           WHERE trade_day < ? AND note_file IS NULL
             AND note_owner_ref IS NOT NULL AND note_id IS NOT NULL""",
        (today.isoformat(),),
    ).fetchone()[0]
    provisional = connection.execute(
        """SELECT COUNT(*) FROM broker_transaction
           WHERE note_file IS NULL AND transaction_id LIKE 'intra-day-%'""",
    ).fetchone()[0]
    return same_day, no_ref, due, provisional


async def sync_trade_archive(
    account: AccountClient,
    from_date: date | None = None,
    to_date: date | None = None,
    *,
    download_notes: bool = True,
    max_notes: int = 30,
    today: date | None = None,
    now: datetime | None = None,
) -> ArchiveSync:
    """Incrementally scan broker transactions and optionally download due PDFs.

    First implicit sync covers 30 days, subsequent calls overlap the last 7
    calendar days to capture late postings/corrections. Explicit date ranges
    permit backfill. All existing older rows/notes remain cached.
    """
    if not isinstance(max_notes, int) or isinstance(max_notes, bool) or not 0 <= max_notes <= _MAX_NOTES_PER_SYNC:
        raise AccountReadError("invalid_note_limit")
    now = now or _utc_now()
    today = today or now.astimezone(_STOCKHOLM).date()
    last = to_date or today
    if last > today or last < date(2000, 1, 1):
        raise AccountReadError("invalid_date_range")

    connection, folder, hint = _open_archive(create=True)
    assert connection is not None
    try:
        previous = _meta(connection, "last_scanned_to")
        if from_date is None:
            if previous is None:
                first = last - timedelta(days=29)
            else:
                previous_day = date.fromisoformat(previous)
                first = min(last, previous_day - timedelta(days=_SYNC_LOOKBACK_DAYS))
        else:
            first = from_date
        if first > last or (last - first).days > _MAX_DATE_SPAN_DAYS:
            raise AccountReadError("invalid_date_range")

        rows, requests, duplicates = await _fetch_transactions(account, first, last)
        with connection:
            inserted, refreshed = _upsert_rows(
                connection, account, rows, now.isoformat()
            )
            if previous is None or last > date.fromisoformat(previous):
                _set_meta(connection, "last_scanned_to", last.isoformat())
            _set_meta(connection, "last_synced_at", now.isoformat())
            connection.execute(
                """INSERT INTO scan_coverage(first_day,last_day,completed_at)
                   VALUES (?,?,?)
                   ON CONFLICT(first_day,last_day)
                   DO UPDATE SET completed_at=excluded.completed_at""",
                (first.isoformat(), last.isoformat(), now.isoformat()),
            )

        attempted = downloaded = reused = failed = deferred = 0
        if download_notes and max_notes:
            attempted, downloaded, reused, failed, deferred = await _download_due_notes(
                account, connection, folder, today, now, max_notes
            )
        same_day, without_ref, awaiting, provisional = _count_notes(connection, today)
        total = connection.execute("SELECT COUNT(*) FROM broker_transaction").fetchone()[0]

        return ArchiveSync(
            from_date=first.isoformat(),
            to_date=last.isoformat(),
            source_scanned_complete=True,
            upstream_requests=requests,
            source_transactions=len(rows),
            duplicates_discarded=duplicates,
            transactions_inserted=inserted,
            transactions_refreshed=refreshed,
            archive_total=total,
            note_attempts=attempted,
            notes_downloaded=downloaded,
            notes_reused=reused,
            notes_failed=failed,
            notes_deferred=deferred,
            notes_pending_publication=same_day,
            notes_awaiting_reference=without_ref,
            provisional_transactions=provisional,
            notes_not_cached=awaiting,
            last_synced_at=now.isoformat(),
            storage_hint=hint,
        )
    finally:
        _close(connection)


def _money_model(amount: str | None, currency: str | None) -> Money | None:
    return Money(amount=amount, currency=currency or "SEK") if amount is not None else None


def _coverage_gaps(
    connection: sqlite3.Connection, first: date, last: date
) -> list[tuple[str, str]]:
    """Return uncovered calendar ranges, not merely missing transaction rows."""
    intervals = connection.execute(
        """SELECT first_day, last_day FROM scan_coverage
           WHERE last_day >= ? AND first_day <= ?
           ORDER BY first_day, last_day""",
        (first.isoformat(), last.isoformat()),
    ).fetchall()
    expected = first
    gaps: list[tuple[str, str]] = []
    for record in intervals:
        start = date.fromisoformat(record["first_day"])
        end = date.fromisoformat(record["last_day"])
        if start > expected:
            gaps.append((
                expected.isoformat(),
                min(last, start - timedelta(days=1)).isoformat(),
            ))
        if end >= expected:
            expected = end + timedelta(days=1)
        if expected > last:
            break
    if expected <= last:
        gaps.append((expected.isoformat(), last.isoformat()))
    return gaps


def read_trade_archive(
    from_date: date, to_date: date, limit: int, offset: int = 0,
) -> ArchivedTrades:
    """Offline broker-history read; auth validation is handled by the worker."""
    if (
        from_date > to_date or (to_date - from_date).days > _MAX_DATE_SPAN_DAYS
        or not 1 <= limit <= 500 or not 0 <= offset <= 100_000
    ):
        raise AccountReadError("invalid_archive_query")
    connection, _folder, hint = _open_archive(create=False)
    if connection is None:
        return ArchivedTrades(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            available=False,
            coverage_complete=False,
            missing_intervals=[(from_date.isoformat(), to_date.isoformat())],
            returned=0,
            total=0,
            has_more=False,
            last_synced_at=None,
            storage_hint=hint,
            transactions=[],
        )
    try:
        total = connection.execute(
            """SELECT COUNT(*) FROM broker_transaction
               WHERE trade_day BETWEEN ? AND ?""",
            (from_date.isoformat(), to_date.isoformat()),
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT transaction_id, side, traded_at, settlement_date,
                      description, instrument_name, isin, order_book_id,
                      volume, amount, amount_currency, commission,
                      commission_currency, trade_day, note_id, note_owner_ref,
                      note_file, note_retry_after, note_error
               FROM broker_transaction
               WHERE trade_day BETWEEN ? AND ?
               ORDER BY trade_day DESC, traded_at DESC, transaction_id
               LIMIT ? OFFSET ?""",
            (from_date.isoformat(), to_date.isoformat(), limit, offset),
        ).fetchall()
        today = _local_today()
        records = []
        for r in rows:
            if r["note_file"]:
                note_state = "SAVED"
            elif date.fromisoformat(r["trade_day"]) >= today:
                note_state = "PENDING_PUBLICATION"
            elif r["transaction_id"].startswith("intra-day-"):
                note_state = "PROVISIONAL"
            elif not r["note_owner_ref"] or not r["note_id"]:
                note_state = "AWAITING_REFERENCE"
            elif r["note_retry_after"]:
                note_state = "RETRY_SCHEDULED"
            else:
                note_state = "READY"
            records.append(
                ArchivedTransaction(
                    transaction_id=r["transaction_id"],
                    transaction_type=r["side"],
                    date=r["traded_at"],
                    settlement_date=r["settlement_date"],
                    description=r["description"],
                    instrument_name=r["instrument_name"],
                    isin=r["isin"],
                    order_book_id=r["order_book_id"],
                    volume=r["volume"],
                    amount=_money_model(r["amount"], r["amount_currency"]),
                    commission=_money_model(r["commission"], r["commission_currency"]),
                    note_status=note_state,
                    note_saved=(note_state == "SAVED"),
                )
            )
        missing_intervals = _coverage_gaps(connection, from_date, to_date)
        return ArchivedTrades(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            available=True,
            coverage_complete=(len(missing_intervals) == 0),
            missing_intervals=missing_intervals[:20],
            returned=len(records),
            total=total,
            has_more=offset + len(records) < total,
            last_synced_at=_meta(connection, "last_synced_at"),
            storage_hint=hint,
            transactions=records,
        )
    finally:
        _close(connection)
