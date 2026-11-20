"""Parse per-fill execution times from cached Avanza contract-note PDFs.

Runs only inside the local worker/process that owns the cached notes. Nothing
here touches the network, and neither raw text nor personal identifiers leave
this module: callers get typed fills plus a reason code.

Verified layout (2026-11-20, 296 real notes): an Avanza "Avräkningsnota" is a
PDF form. Field values live in widget annotations on each page; multi-page
notes have no AcroForm catalog and continue the fill table on page 2+ under
``instrument_page2_<col>_<row>_value``. Column meaning comes from the
``instrument[_page2]_<col>_label`` texts (Antal, Kurs, Valuta, Tid, Ordertyp,
MP). ``Tid`` is ``YYMMDD HH:MM:SS`` on the Europe/Stockholm wall clock: it
matched all 61 intraday API timestamps to the second, and Stockholm-listed
fills start at 09:00:0x in summer. The document carries other dates
(Affärsdatum, Likviddatum, Utskriftsdatum, PDF metadata) that are never used
as an execution time.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

PARSER_VERSION = "avanza-note-form-v1"
TIMEZONE = "Europe/Stockholm"
_STOCKHOLM = ZoneInfo(TIMEZONE)
_MAX_PAGES = 20
_MAX_WIDGETS = 5000
_MAX_FILLS = 1000
_MAX_VALUE_CHARS = 512
_TIME = re.compile(r"^(\d{2})(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})$")
_FILL_CELL = re.compile(r"^instrument_(page2_)?(\d+)_(\d+)_value$")
_LABEL_CELL = re.compile(r"^instrument_(page2_)?(\d+)_label$")
_COLUMNS = {"Antal": "quantity", "Kurs": "price", "Valuta": "currency",
            "Tid": "time", "Ordertyp": "order_type", "MP": "venue"}
_EMPTY = {"", "-"}


class NoteParseError(Exception):
    """Reason code only; never carries document text."""


@dataclass(frozen=True)
class Fill:
    index: int
    quantity: Decimal
    price: Decimal
    currency: str
    local_time: datetime | None  # naive Stockholm wall clock
    venue: str | None
    order_type: str | None


@dataclass(frozen=True)
class ParsedNote:
    placeholder: bool
    isin: str | None
    side: str | None
    business_date: date | None
    total_quantity: Decimal | None
    average_price: Decimal | None
    fills: tuple[Fill, ...]


def _decimal(text: str) -> Decimal:
    try:
        value = Decimal(text.replace("\xa0", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise NoteParseError("invalid_number") from None
    if not value.is_finite():
        raise NoteParseError("invalid_number")
    return value


def _widgets(data: bytes) -> list[dict[str, str]]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise NoteParseError("encrypted_pdf")
        if len(reader.pages) > _MAX_PAGES:
            raise NoteParseError("too_many_pages")
        pages: list[dict[str, str]] = []
        count = 0
        for page in reader.pages:
            fields: dict[str, str] = {}
            for ref in page.get("/Annots") or []:
                annotation = ref.get_object()
                count += 1
                if count > _MAX_WIDGETS:
                    raise NoteParseError("too_many_fields")
                parent = annotation.get("/Parent")
                parent = parent.get_object() if parent is not None else {}
                name = annotation.get("/T", parent.get("/T"))
                value = annotation.get("/V", parent.get("/V"))
                if name is None:
                    continue
                name, value = str(name), "" if value is None else str(value)
                if len(name) > 128 or len(value) > _MAX_VALUE_CHARS:
                    raise NoteParseError("oversized_field")
                if name in fields and fields[name] != value:
                    raise NoteParseError("conflicting_field")
                fields[name] = value.strip()
            pages.append(fields)
        return pages
    except NoteParseError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise NoteParseError("unreadable_pdf") from None


def _side(headline: str) -> str | None:
    text = headline.lower()
    bought, sold = "köpt" in text, "sålt" in text
    return "BUY" if bought and not sold else "SELL" if sold and not bought else None


def _local_time(raw: str) -> datetime | None:
    match = _TIME.fullmatch(raw)
    if match is None:
        return None
    yy, mm, dd, hh, mi, ss = (int(part) for part in match.groups())
    try:
        return datetime(2000 + yy, mm, dd, hh, mi, ss)
    except ValueError:
        raise NoteParseError("invalid_fill_time") from None


def parse_note(data: bytes) -> ParsedNote:
    """Return the fills of one note; raise NoteParseError on unusable input."""
    if not data.startswith(b"%PDF-"):
        raise NoteParseError("not_a_pdf")
    pages = _widgets(data)
    if not pages or not pages[0]:
        raise NoteParseError("no_form_fields")  # e.g. scanned/flattened note; no OCR
    head = pages[0]
    isin = head.get("isin_number", "")
    business = head.get("business_date", "")
    if isin in _EMPTY and business in _EMPTY:
        # Avanza served a "this note could not be ..." stub without trade data.
        return ParsedNote(True, None, None, None, None, None, ())

    misc = {
        head[name]: head.get(name[: -len("label")] + "value", "")
        for name in head if re.fullmatch(r"misc_\d+_label", name) and head[name]
    }
    total = misc.get("Totalt antal")
    average = next((v for k, v in misc.items() if k.startswith("Volymviktad snittkurs")), None)
    try:
        business_date = date.fromisoformat(business)
    except ValueError:
        business_date = None

    fills: list[Fill] = []
    layout = False
    for fields in pages:
        columns: dict[tuple[bool, int], str] = {}
        for name, value in fields.items():
            label = _LABEL_CELL.fullmatch(name)
            if label and value in _COLUMNS:
                columns[(bool(label[1]), int(label[2]))] = _COLUMNS[value]
                layout = True
        cells: dict[tuple[bool, int], dict[str, str]] = {}
        for name, value in fields.items():
            cell = _FILL_CELL.fullmatch(name)
            if cell is None:
                continue
            column = columns.get((bool(cell[1]), int(cell[2])))
            if column is not None:
                cells.setdefault((bool(cell[1]), int(cell[3])), {})[column] = value
        for key in sorted(cells):
            row = cells[key]
            if row.get("quantity", "") in _EMPTY:
                continue
            if row.get("price", "") in _EMPTY or row.get("currency", "") in _EMPTY:
                raise NoteParseError("incomplete_fill_row")
            if len(fills) >= _MAX_FILLS:
                raise NoteParseError("too_many_fills")
            fills.append(Fill(
                index=len(fills) + 1,
                quantity=_decimal(row["quantity"]),
                price=_decimal(row["price"]),
                currency=row["currency"],
                local_time=_local_time(row.get("time", "")),
                venue=None if row.get("venue", "") in _EMPTY else row["venue"],
                order_type=None if row.get("order_type", "") in _EMPTY else row["order_type"],
            ))
    if not layout:
        raise NoteParseError("unknown_layout")
    return ParsedNote(
        placeholder=False,
        isin=None if isin in _EMPTY else isin,
        side=_side(head.get("instrument_headline", "")),
        business_date=business_date,
        total_quantity=None if total is None else _decimal(total),
        average_price=None if average is None else _decimal(average),
        fills=tuple(fills),
    )


def stockholm_instant(local: datetime) -> tuple[datetime, str] | None:
    """Return (aware local time, utc iso) or None for a DST-ambiguous/nonexistent clock time."""
    first = local.replace(tzinfo=_STOCKHOLM, fold=0)
    second = local.replace(tzinfo=_STOCKHOLM, fold=1)
    if first.utcoffset() != second.utcoffset():
        return None  # autumn hour that occurs twice, or the skipped spring hour
    if first.astimezone(timezone.utc).astimezone(_STOCKHOLM).replace(tzinfo=None) != local:
        return None
    return first, first.astimezone(timezone.utc).isoformat()


def reconcile(
    parsed: ParsedNote, *, isin: str | None, side: str, volume: str | None,
) -> tuple[str, str | None]:
    """Status/reason for a note against its broker transaction; fails closed."""
    if parsed.placeholder:
        return "DATE_ONLY", "note_placeholder"
    if parsed.isin != isin:
        return "AMBIGUOUS", "isin_mismatch"
    if parsed.side != side:
        return "AMBIGUOUS", "side_mismatch"
    if not parsed.fills:
        return "DATE_ONLY", "no_fill_rows"
    try:
        expected = abs(Decimal(volume)) if volume is not None else None
    except InvalidOperation:
        expected = None
    filled = sum((f.quantity for f in parsed.fills), Decimal(0))
    if any(f.quantity <= 0 or f.price <= 0 for f in parsed.fills):
        return "AMBIGUOUS", "non_positive_fill"
    if expected is None or filled != expected or parsed.total_quantity != expected:
        return "AMBIGUOUS", "quantity_mismatch"
    if parsed.average_price is not None and len({f.currency for f in parsed.fills}) == 1:
        vwap = sum((f.quantity * f.price for f in parsed.fills), Decimal(0)) / filled
        # Fill prices are printed rounded; the declared average carries more decimals.
        if abs(vwap - parsed.average_price) > parsed.average_price * Decimal("0.001"):
            return "AMBIGUOUS", "price_mismatch"
    if any(f.local_time is None for f in parsed.fills):
        return "DATE_ONLY", "fill_time_missing"
    if parsed.business_date is None or any(
        f.local_time.date() != parsed.business_date for f in parsed.fills  # type: ignore[union-attr]
    ):
        return "AMBIGUOUS", "fill_date_mismatch"
    if any(stockholm_instant(f.local_time) is None for f in parsed.fills):  # type: ignore[arg-type]
        return "AMBIGUOUS", "dst_ambiguous_time"
    return "VERIFIED", None

