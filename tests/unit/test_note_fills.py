"""Synthetic contract-note fill extraction tests.

Every PDF here is generated from fake values; no real note, account or person.
"""
from __future__ import annotations

import io
import sqlite3
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject,
)

from avanza_mcp.client.note_fills import NoteParseError, parse_note, stockholm_instant
from avanza_mcp.client.trade_archive import (
    extract_note_fills, main, read_trade_archive, sync_trade_archive,
)

from .test_trade_archive import FakeAvanza, _transaction, archive_folder  # noqa: F401

_LABELS = {1: "Antal", 3: "Kurs", 4: "Valuta", 5: "Tid", 6: "Ordertyp", 7: "MP", 8: "HK"}


def _pdf(pages: list[dict[str, str]]) -> bytes:
    writer = PdfWriter()
    for number, fields in enumerate(pages):
        writer.add_blank_page(595, 842)
        for name, value in fields.items():
            writer.add_annotation(number, DictionaryObject({
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/Rect"): ArrayObject([FloatObject(0)] * 4),
                NameObject("/T"): TextStringObject(name),
                NameObject("/V"): TextStringObject(value),
            }))
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _rows(prefix: str, fills: list[tuple[str, str, str]], currency="SEK") -> dict[str, str]:
    out = {f"{prefix}{col}_label": text for col, text in _LABELS.items()}
    for row, (qty, price, when) in enumerate(fills, start=1):
        out |= {
            f"{prefix}1_{row}_value": qty, f"{prefix}3_{row}_value": price,
            f"{prefix}4_{row}_value": currency, f"{prefix}5_{row}_value": when,
            f"{prefix}6_{row}_value": "L", f"{prefix}7_{row}_value": "XTST",
            f"{prefix}8_{row}_value": "K",
        }
    return out


def note(
    *, isin="SE0000000001", side="BUY", day="2026-11-12",
    fills=(("10", "12,00", "261112 09:15:01"),), page2=(), total=None, average=None,
) -> bytes:
    verb = "köpt" if side == "BUY" else "sålt"
    quantity = sum(int(q.replace(" ", "")) for q, _, _ in (*fills, *page2))
    first = {
        "instrument_headline": f"Enligt uppdrag har vi för Er räkning {verb} följande värdepapper:",
        "instrument_name": "SYNTHETIC TEST INSTRUMENT",
        "isin_number": isin,
        "business_date": day,
        "payment_date": day,
        "print_date": day,
        "depot_owner": "Test Person",
        "depot_number": "DEPOT-SYNTH-9",
        "misc_1_label": "Totalt antal",
        "misc_1_value": total or str(quantity),
        "misc_2_label": "Volymviktad snittkurs (SEK)",
        "misc_2_value": average or "12,00",
    } | _rows("instrument_", list(fills))
    pages = [first]
    if page2:
        pages.append({"isin_number_page2": isin} | _rows("instrument_page2_", list(page2)))
    return _pdf(pages)


_PLACEHOLDER = _pdf([{
    "instrument_name": "Denna nota kunde inte läsas upp", "isin_number": "-",
    "business_date": "-", "print_date": "2026-11-20", "instrument_5_label": "Tid",
    "instrument_5_1_value": "-",
}])


class NoteServer(FakeAvanza):
    def __init__(self, rows, documents):
        super().__init__(rows)
        self.documents = documents

    async def request_authenticated_note(self, path):
        self.pdf_calls.append(path)
        name = path.rsplit("/", 2)[-2]
        return httpx.Response(
            200, content=self.documents[name], headers={"content-type": "application/pdf"}
        )


def _trade(n, day, note_id, *, side="BUY", volume="10", isin="SE0000000001"):
    row = _transaction(n, day, note=note_id)
    row["type"] = side
    row["volume"] = {"value": volume if side == "BUY" else f"-{volume}"}
    row["isin"] = isin
    row["date"] = f"{day}T00:00:00"
    return row


async def _sync(rows, documents, day="2026-11-12", today=date(2026, 11, 20)):
    source = NoteServer(rows, documents)
    result = await sync_trade_archive(
        source, date.fromisoformat(day), date.fromisoformat(day),
        today=today, now=datetime.fromisoformat("2026-11-20T09:00:00+00:00"),
    )
    history = read_trade_archive(date.fromisoformat(day), date.fromisoformat(day), 100)
    return source, result, {t.transaction_id: t for t in history.transactions}, history


# --- parser --------------------------------------------------------------


def test_single_fill_is_parsed_with_its_own_columns():
    parsed = parse_note(note())
    assert parsed.isin == "SE0000000001" and parsed.side == "BUY"
    assert [(f.quantity, f.price, f.local_time) for f in parsed.fills] == [
        (Decimal(10), Decimal("12.00"), datetime(2026, 11, 12, 9, 15, 1))
    ]


def test_multi_page_continuation_rows_are_kept_in_order():
    parsed = parse_note(note(
        fills=(("1 000", "12,00", "261112 09:15:01"), ("500", "12,10", "261112 09:15:01")),
        page2=(("250", "12,20", "261112 10:00:00"),),
    ))
    assert [f.index for f in parsed.fills] == [1, 2, 3]
    assert [f.quantity for f in parsed.fills] == [1000, 500, 250]


@pytest.mark.parametrize("data", [b"not a pdf", b"%PDF-1.4\ngarbage\n%%EOF", _pdf([{}])])
def test_unreadable_or_formless_pdf_raises_a_reason_code(data):
    with pytest.raises(NoteParseError):
        parse_note(data)


def test_dst_ambiguous_and_skipped_clock_times_are_refused():
    assert stockholm_instant(datetime(2026, 10, 25, 2, 30)) is None  # occurs twice
    assert stockholm_instant(datetime(2026, 3, 29, 2, 30)) is None  # does not exist
    summer, _ = stockholm_instant(datetime(2026, 10, 24, 9, 0))
    winter, utc = stockholm_instant(datetime(2026, 10, 26, 9, 0))
    assert summer.isoformat().endswith("+02:00") and winter.isoformat().endswith("+01:00")
    assert utc == "2026-10-26T08:00:00+00:00"


# --- archive -------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_extracts_verified_fills_without_leaking_private_refs(archive_folder):
    _, result, rows, history = await _sync(
        [_trade(1, "2026-11-12", "note-a")], {"note-a": note()}
    )
    tx = rows["trade-1"]
    assert result.notes_extracted == 1
    assert tx.execution_status == "VERIFIED" and tx.execution_reason is None
    assert [f.executed_at for f in tx.execution_fills] == ["2026-11-12T09:15:01+01:00"]
    assert tx.execution_fills[0].executed_at_utc == "2026-11-12T08:15:01+00:00"
    assert tx.date == "2026-11-12T00:00:00" and tx.volume == "10"  # broker facts untouched
    assert history.execution_status_counts == {"VERIFIED": 1}
    dump = history.model_dump_json()
    for private in ("synthetic-user", "note-a", "fake-acc", "Test Person", "DEPOT-SYNTH-9", ".pdf"):
        assert private not in dump


@pytest.mark.asyncio
async def test_same_isin_same_day_trades_keep_their_own_fills(archive_folder):
    _, _, rows, _ = await _sync(
        [_trade(1, "2026-11-12", "note-a"), _trade(2, "2026-11-12", "note-b", side="SELL")],
        {
            "note-a": note(fills=(("10", "12,00", "261112 09:15:01"),)),
            "note-b": note(side="SELL", fills=(("10", "12,00", "261112 15:30:00"),)),
        },
    )
    assert rows["trade-1"].execution_fills[0].executed_at.startswith("2026-11-12T09:15:01")
    assert rows["trade-2"].execution_fills[0].executed_at.startswith("2026-11-12T15:30:00")


@pytest.mark.asyncio
async def test_summer_time_fill_gets_cest_offset(archive_folder):
    _, _, rows, _ = await _sync(
        [_trade(1, "2026-08-14", "note-a")],
        {"note-a": note(day="2026-08-14", fills=(("10", "12,00", "260814 09:00:04"),))},
        day="2026-08-14",
    )
    assert rows["trade-1"].execution_fills[0].executed_at == "2026-08-14T09:00:04+02:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(("document", "status", "reason"), [
    (note(fills=(("10", "12,00", "-"),)), "DATE_ONLY", "fill_time_missing"),
    (note(total="11"), "AMBIGUOUS", "quantity_mismatch"),
    (note(fills=(("9", "12,00", "261112 09:15:01"),)), "AMBIGUOUS", "quantity_mismatch"),
    (note(average="15,00"), "AMBIGUOUS", "price_mismatch"),
    (note(isin="SE0000000002"), "AMBIGUOUS", "isin_mismatch"),
    (note(side="SELL"), "AMBIGUOUS", "side_mismatch"),
    (note(fills=(("10", "12,00", "261111 23:59:59"),)), "AMBIGUOUS", "fill_date_mismatch"),
    (b"%PDF-1.4\ngarbage\n%%EOF\n", "MISSING", "unreadable_pdf"),
])
async def test_conflicts_fail_closed_without_fills(archive_folder, document, status, reason):
    _, _, rows, _ = await _sync([_trade(1, "2026-11-12", "note-a")], {"note-a": document})
    tx = rows["trade-1"]
    assert (tx.execution_status, tx.execution_reason, tx.execution_fills) == (status, reason, [])


@pytest.mark.asyncio
async def test_dst_ambiguous_fill_is_ambiguous(archive_folder):
    _, _, rows, _ = await _sync(
        [_trade(1, "2026-10-25", "note-a")],
        {"note-a": note(day="2026-10-25", fills=(("10", "12,00", "261025 02:30:00"),))},
        day="2026-10-25",
    )
    assert rows["trade-1"].execution_status == "AMBIGUOUS"
    assert rows["trade-1"].execution_reason == "dst_ambiguous_time"


@pytest.mark.asyncio
async def test_note_dated_before_api_settlement_date_is_verified_and_flagged(archive_folder):
    _, _, rows, _ = await _sync(
        [_trade(1, "2026-10-12", "note-a")],
        {"note-a": note(day="2026-10-09", fills=(("10", "12,00", "261009 15:57:52"),))},
        day="2026-10-12",
    )
    tx = rows["trade-1"]
    assert tx.execution_status == "VERIFIED"
    assert tx.execution_reason == "trade_date_differs_from_api"
    assert tx.execution_fills[0].executed_at == "2026-10-09T15:57:52+02:00"


@pytest.mark.asyncio
async def test_recycled_note_reference_is_ambiguous_for_every_holder(archive_folder):
    _, _, rows, _ = await _sync(
        [_trade(1, "2026-11-12", "note-a"), _trade(2, "2026-11-12", "note-a")],
        {"note-a": note()},
    )
    assert {t.execution_status for t in rows.values()} == {"AMBIGUOUS"}
    assert {t.execution_reason for t in rows.values()} == {"note_shared_by_transactions"}


@pytest.mark.asyncio
async def test_provisional_intraday_rows_stay_distinct(archive_folder):
    intraday = _trade(1, "2026-11-12", None)
    intraday["id"] = "intra-day-1"
    intraday["date"] = "2026-11-12T09:15:01.123"
    _, _, rows, _ = await _sync([intraday, _trade(2, "2026-11-12", "note-a")], {"note-a": note()})
    assert rows["intra-day-1"].execution_status == "PROVISIONAL"
    assert rows["intra-day-1"].execution_fills == []
    assert rows["trade-2"].execution_status == "VERIFIED"


@pytest.mark.asyncio
async def test_placeholder_note_is_requeued_and_refetched(archive_folder):
    trade = _trade(1, "2026-11-12", "note-a")
    source, _, rows, _ = await _sync([trade], {"note-a": _PLACEHOLDER})
    assert rows["trade-1"].execution_status == "MISSING"
    assert rows["trade-1"].execution_reason == "note_placeholder"
    assert rows["trade-1"].note_status == "RETRY_SCHEDULED"
    assert len(list((archive_folder / "notes").glob("*.placeholder-*.pdf"))) == 1

    source.documents["note-a"] = note()
    await sync_trade_archive(
        source, date(2026, 11, 12), date(2026, 11, 12),
        today=date(2026, 11, 21), now=datetime.fromisoformat("2026-11-21T09:00:00+00:00"),
    )
    tx = read_trade_archive(date(2026, 11, 12), date(2026, 11, 12), 10).transactions[0]
    assert len(source.pdf_calls) == 2
    assert tx.execution_status == "VERIFIED" and tx.note_status == "SAVED"


@pytest.mark.asyncio
async def test_repeated_extraction_is_idempotent(archive_folder, capsys):
    await _sync(
        [_trade(1, "2026-11-12", "note-a")],
        {"note-a": note(fills=(("4", "12,00", "261112 09:15:01"), ("6", "12,00", "261112 09:16:00")))},
    )
    database = archive_folder / "trade_archive.sqlite3"
    before = sqlite3.connect(database).execute(
        "SELECT COUNT(*) FROM note_extraction UNION ALL SELECT COUNT(*) FROM execution_fill"
    ).fetchall()
    assert main(["backfill"]) == 0
    assert '"VERIFIED": 0' in capsys.readouterr().out
    connection = sqlite3.connect(database)
    assert extract_note_fills(connection, archive_folder / "notes", datetime.now())["fills"] == 0
    after = connection.execute(
        "SELECT COUNT(*) FROM note_extraction UNION ALL SELECT COUNT(*) FROM execution_fill"
    ).fetchall()
    assert before == after == [(1,), (2,)]


@pytest.mark.asyncio
async def test_export_writes_complete_archive_result(archive_folder, tmp_path):
    await _sync([_trade(1, "2026-11-12", "note-a")], {"note-a": note()})
    out = tmp_path / "export.json"
    assert main(["export", "--from", "2026-11-12", "--to", "2026-11-12", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert '"has_more": false' in text and '"VERIFIED"' in text
    assert "synthetic-user" not in text and "note-a" not in text
