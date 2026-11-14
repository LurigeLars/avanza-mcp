"""Synthetic regression tests for the local Avanza evidence archive.

Never use real account IDs, broker documents, credentials or trades in CI.
"""
from datetime import date, datetime, timezone
import httpx
import pytest

from avanza_mcp.client.accounts import AccountClient, AccountReadError
from avanza_mcp.client.trade_archive import read_trade_archive, sync_trade_archive

_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def _transaction(n: int, day: str, *, note: str | None = None):
    item = {
        "id": f"trade-{n}",
        "account": {"id": "fake-acc", "urlParameterId": "synthetic-user"},
        "type": "BUY" if n % 2 else "SELL",
        "description": f"Synthetic transaction {n}",
        "date": f"{day}T12:34:56",
        "settlementDate": day,
        "instrumentName": "SYNTHETIC TEST INSTRUMENT",
        "isin": "SE0000000001",
        "orderbook": {"id": 123},
        "volume": {"value": "10"},
        "amount": {"value": "-120.00", "unit": "SEK"},
        "commission": {"value": "1.00", "unit": "SEK"},
    }
    if note is not None:
        item["noteId"] = note
    return item


class TestAvanza:
    def __init__(self, rows):
        self.rows = list(rows)
        self.transaction_calls = []
        self.pdf_calls = []
        self.pdf_status = 200
        self._client = self

    def _transaction(self, row):
        return AccountClient._transaction(row)

    async def _get(self, path, **kwargs):
        assert path == "/_api/transactions/list"
        params = kwargs["params"]
        assert params["transactionTypes"] == "BUY,SELL"
        assert params["maxElements"] == 1000
        self.transaction_calls.append(params)
        selected = [
            row for row in self.rows
            if params["from"] <= row["date"][:10] <= params["to"]
        ]
        return {
            "transactions": selected[:1000],
            "transactionsAfterFiltering": len(selected),
        }

    async def request_authenticated(self, method, path):
        assert method == "GET"
        assert path.startswith("/_api/contract-notes/documents/synthetic-user/")
        self.pdf_calls.append(path)
        if self.pdf_status == 404:
            return httpx.Response(
                404, content=b"missing",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(
            200, content=_PDF,
            headers={"content-type": "application/pdf"},
        )


@pytest.fixture
def archive_folder(tmp_path, monkeypatch):
    folder = tmp_path / "notes"
    folder.mkdir()
    location = (tmp_path / "trade_archive.sqlite3", folder, "private-test-folder")
    monkeypatch.setattr(
        "avanza_mcp.client.trade_archive._archive_location",
        lambda: location,
    )
    return tmp_path


def _at(day: str, hour: int = 9):
    return datetime.fromisoformat(f"{day}T{hour:02}:00:00+00:00")


@pytest.mark.asyncio
async def test_first_sync_persists_trade_and_deferred_same_day_note(archive_folder):
    source = TestAvanza([_transaction(1, "2026-11-13", note="note-one")])
    result = await sync_trade_archive(
        source, from_date=date(2026, 11, 13),
        to_date=date(2026, 11, 13), today=date(2026, 11, 13),
        now=_at("2026-11-13"),
    )
    assert result.archive_total == 1 and result.transactions_inserted == 1
    assert result.notes_pending_publication == 1
    assert result.notes_downloaded == 0 and source.pdf_calls == []
    assert (archive_folder / "trade_archive.sqlite3").exists()


@pytest.mark.asyncio
async def test_notes_mature_next_day_and_are_never_fetched_twice(archive_folder):
    source = TestAvanza([_transaction(1, "2026-11-13", note="note-one")])
    first = await sync_trade_archive(
        source, date(2026, 11, 13), date(2026, 11, 13),
        today=date(2026, 11, 14), now=_at("2026-11-14"),
    )
    assert first.notes_downloaded == 1
    assert len(source.pdf_calls) == 1
    files = list((archive_folder / "notes").glob("*.pdf"))
    assert len(files) == 1 and files[0].read_bytes() == _PDF

    next_sync = await sync_trade_archive(
        source, date(2026, 11, 13), date(2026, 11, 13),
        today=date(2026, 11, 14), now=_at("2026-11-14", 10),
    )
    assert next_sync.transactions_inserted == 0
    assert next_sync.transactions_refreshed == 1
    assert len(source.pdf_calls) == 1  # Nothing downloaded again.
    history = read_trade_archive(date(2026, 11, 13), date(2026, 11, 13), 100)
    assert history.available and history.total == 1
    assert history.transactions[0].note_saved is True
    assert history.transactions[0].note_status == "SAVED"
    assert "synthetic-user" not in history.model_dump_json()
    assert "note-one" not in history.model_dump_json()


@pytest.mark.asyncio
async def test_late_note_reference_is_caught_by_rolling_rescan(archive_folder):
    source = TestAvanza([_transaction(1, "2026-11-12")])
    first = await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 13),
        today=date(2026, 11, 13), now=_at("2026-11-13"),
    )
    assert first.notes_awaiting_reference == 1
    assert first.notes_downloaded == 0

    source.rows = [_transaction(1, "2026-11-12", note="late-created-note")]
    second = await sync_trade_archive(
        source, today=date(2026, 11, 14), now=_at("2026-11-14")
    )
    assert second.from_date == "2026-11-07"
    assert second.notes_downloaded == 1
    assert second.archive_total == 1


@pytest.mark.asyncio
async def test_404_is_retained_and_retried_only_following_day(archive_folder):
    source = TestAvanza([_transaction(1, "2026-11-12", note="note-one")])
    source.pdf_status = 404
    one = await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 12),
        today=date(2026, 11, 14), now=_at("2026-11-14"),
    )
    assert one.notes_failed == 1
    assert len(source.pdf_calls) == 1
    source.pdf_status = 200
    two = await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 12),
        today=date(2026, 11, 14), now=_at("2026-11-14", 10),
    )
    assert two.notes_downloaded == 0 and len(source.pdf_calls) == 1
    three = await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 12),
        today=date(2026, 11, 15), now=_at("2026-11-15", 2),
    )
    assert three.notes_downloaded == 1 and len(source.pdf_calls) == 2


@pytest.mark.asyncio
async def test_reuses_already_downloaded_note_across_two_transactions(archive_folder):
    source = TestAvanza([
        _transaction(1, "2026-11-12", note="group-note"),
        _transaction(2, "2026-11-12", note="group-note"),
    ])
    one = await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 12),
        today=date(2026, 11, 14), now=_at("2026-11-14"),
    )
    assert one.archive_total == 2
    assert one.notes_downloaded == 1
    assert len(source.pdf_calls) == 1


@pytest.mark.asyncio
async def test_cache_read_never_contacts_avanza_and_is_paginated(archive_folder):
    source = TestAvanza([_transaction(i, "2026-11-12") for i in range(1, 9)])
    await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 12),
        download_notes=False, today=date(2026, 11, 14),
        now=_at("2026-11-14"),
    )
    calls_before = len(source.transaction_calls)
    first_page = read_trade_archive(date(2026, 11, 12), date(2026, 11, 12), 3)
    second_page = read_trade_archive(date(2026, 11, 12), date(2026, 11, 12), 3, 3)
    assert first_page.total == 8
    assert first_page.returned == 3 and first_page.has_more
    assert second_page.returned == 3
    assert len(source.transaction_calls) == calls_before
    assert first_page.transactions[0].amount.currency == "SEK"


@pytest.mark.asyncio
async def test_source_overflow_same_day_fails_without_committing(archive_folder):
    source = TestAvanza([
        _transaction(i, "2026-11-12") for i in range(1, 1002)
    ])
    with pytest.raises(AccountReadError, match="single_day_exceeds_1000"):
        await sync_trade_archive(
            source, date(2026, 11, 12), date(2026, 11, 12),
            today=date(2026, 11, 14), now=_at("2026-11-14"),
        )
    history = read_trade_archive(date(2026, 11, 12), date(2026, 11, 12), 10)
    assert history.total == 0


@pytest.mark.asyncio
async def test_bisects_overfull_range_without_gaps(archive_folder):
    rows = [
        *[_transaction(i, "2026-11-12") for i in range(1, 801)],
        *[_transaction(i, "2026-11-13") for i in range(801, 1301)],
    ]
    source = TestAvanza(rows)
    result = await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 13),
        download_notes=False, today=date(2026, 11, 14),
        now=_at("2026-11-14"),
    )
    assert result.archive_total == 1300
    assert result.upstream_requests == 3


@pytest.mark.asyncio
async def test_bounded_note_attempts_and_recovers_without_rereading_history(archive_folder):
    source = TestAvanza([
        _transaction(i, "2026-10-12", note=f"note-{i}")
        for i in range(1, 6)
    ])
    first = await sync_trade_archive(
        source, date(2026, 10, 12), date(2026, 10, 12),
        max_notes=2, today=date(2026, 11, 14), now=_at("2026-11-14"),
    )
    assert first.note_attempts == 2 and first.notes_not_cached == 3
    next_run = await sync_trade_archive(
        source, max_notes=3, today=date(2026, 11, 14),
        now=_at("2026-11-14", 10),
    )
    assert next_run.notes_downloaded == 3
    assert next_run.from_date == "2026-11-07"
    assert next_run.archive_total == 5


def test_no_archive_yields_explicit_unavailable_status(archive_folder):
    result = read_trade_archive(date(2026, 11, 10), date(2026, 11, 14), 30)
    assert result.available is False and result.transactions == []


def test_query_validation_and_database_bounds(archive_folder):
    with pytest.raises(AccountReadError, match="invalid_archive_query"):
        read_trade_archive(date(2026, 11, 15), date(2026, 11, 14), 10)
    with pytest.raises(AccountReadError, match="invalid_archive_query"):
        read_trade_archive(date(2026, 11, 13), date(2026, 11, 14), 1000)
