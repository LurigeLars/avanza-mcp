"""Download Avanza contract-note PDFs inside the isolated authenticated worker.

No raw document, account URL ID, note ID, or absolute filesystem path crosses
the MCP worker boundary. Avanza's undocumented API may change without notice.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from ..models.account import ContractNoteExport
from .accounts import AccountAuthExpired, AccountReadError, _account_request_allowed
from .exceptions import AvanzaAuthError
from .note_policy import MAX_PDF_BYTES as _MAX_PDF_BYTES, NoteReadError

if TYPE_CHECKING:
    from .accounts import AccountClient

_NOTE_ROUTE = "/_api/contract-notes/documents/{account}/{note}/note.pdf"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$", re.ASCII)
_MAX_NOTES = 200
_MAX_EXPORT_BYTES = 50 * 1024 * 1024
_MAX_SCAN_REQUESTS = 64


def _note_directory() -> tuple[Path, str]:
    """Choose a fixed private *user-local* location, not a caller-supplied path."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        folder = base / "AvanzaMCP" / "contract-notes"
        hint = r"%LOCALAPPDATA%\AvanzaMCP\contract-notes"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        folder = base / "avanza-mcp" / "contract-notes"
        hint = "~/.local/share/avanza-mcp/contract-notes"
    # Do not follow a symlink at the leaf directory.
    if folder.is_symlink():
        raise AccountReadError("unsafe_storage")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink():
        raise AccountReadError("unsafe_storage")
    return folder, hint


def _date_of(item: dict[str, Any]) -> date:
    value = item.get("date") or item.get("tradeDate")
    if not isinstance(value, str) or len(value) < 10:
        raise AccountReadError("invalid_transaction_date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise AccountReadError("invalid_transaction_date") from None


async def _scan_transactions(
    account: AccountClient, from_date: date, to_date: date
) -> tuple[list[dict[str, Any]], int]:
    """Bisect date ranges if Avanza reports over 200 trades in one range.

    No pagination offset is assumed. An overflowing *single day* fails closed.
    """
    requests = 0

    async def scan(start: date, end: date) -> list[dict[str, Any]]:
        nonlocal requests
        requests += 1
        if requests > _MAX_SCAN_REQUESTS:
            raise AccountReadError("scan_limit")
        response = await account._get(
            "/_api/transactions/list",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "maxElements": _MAX_NOTES + 1,
                "transactionTypes": "BUY,SELL",
            },
        )
        rows = response.get("transactions")
        count = response.get("transactionsAfterFiltering")
        if not isinstance(rows, list) or not isinstance(count, int) or count < 0:
            raise AccountReadError("invalid_transaction_list")
        if count > _MAX_NOTES or len(rows) > _MAX_NOTES:
            if start == end:
                raise AccountReadError("single_day_exceeds_200")
            half = start + timedelta(days=(end - start).days // 2)
            return (await scan(start, half)) + (
                await scan(half + timedelta(days=1), end)
            )
        if count != len(rows) or not all(isinstance(row, dict) for row in rows):
            raise AccountReadError("incomplete_transaction_list")
        return rows

    values = await scan(from_date, to_date)
    # Upstream may return the same transaction on adjacent calls. De-duplicate
    # by its broker ID; do not infer identity from amount/size.
    unique: dict[str, dict[str, Any]] = {}
    for item in values:
        identity = item.get("id")
        if not isinstance(identity, (str, int)) or isinstance(identity, bool):
            raise AccountReadError("missing_transaction_id")
        key = str(identity)
        if key not in unique:
            unique[key] = item
    return sorted(unique.values(), key=lambda row: (_date_of(row), str(row["id"]))), (
        len(values) - len(unique)
    )


def _select_200_whole_days(
    values: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], date | None]:
    selected: list[dict[str, Any]] = []
    next_date: date | None = None
    current_day: date | None = None
    day_rows: list[dict[str, Any]] = []

    def add_day(day: date, rows: list[dict[str, Any]]) -> bool:
        if len(rows) > _MAX_NOTES:
            raise AccountReadError("single_day_exceeds_200")
        if len(selected) + len(rows) > _MAX_NOTES:
            return False
        selected.extend(rows)
        return True

    for item in values:
        day = _date_of(item)
        if current_day is not None and day != current_day:
            if not add_day(current_day, day_rows):
                next_date = current_day
                break
            day_rows = []
        current_day = day
        day_rows.append(item)
    else:
        if current_day is not None and not add_day(current_day, day_rows):
            next_date = current_day

    return selected, next_date


def _classify_redirect(location: str | None) -> str:
    """Categorize a broker redirect without leaking its URL or query tokens.

    This helper intentionally does not follow any redirect. The endpoint's
    destination must first be understood before allowing credentials or
    document identifiers to reach another origin.
    """
    if not location:
        return "redirect_missing_location"
    try:
        parsed = urlsplit(location.strip())
        if any(term in parsed.path.lower() for term in (
            "/login", "/auth", "/bankid", "/session",
        )):
            return "redirect_login"
        if parsed.scheme and parsed.scheme.lower() != "https":
            return "redirect_insecure"
        if parsed.netloc:
            host = parsed.hostname
            if not host:
                return "redirect_invalid_location"
            if host.lower() == "www.avanza.se":
                return "redirect_same_origin"
            if host.lower() == "avanza.se" or host.lower().endswith(".avanza.se"):
                return "redirect_avanza_subdomain"
            return "redirect_external"
        if parsed.path.startswith("/"):
            return "redirect_relative"
        return "redirect_relative"
    except ValueError:
        return "redirect_invalid_location"


async def _read_pdf(account: AccountClient, url_account: str, note_id: str) -> bytes:
    if not _SAFE_ID.fullmatch(url_account) or not _SAFE_ID.fullmatch(note_id):
        raise AccountReadError("invalid_note_reference")
    path = _NOTE_ROUTE.format(account=url_account, note=note_id)
    if not _account_request_allowed("GET", path):
        raise AccountReadError("request_not_allowed")
    try:
        response = await account._client.request_authenticated_note(path)
    except AvanzaAuthError:
        raise AccountAuthExpired from None
    except NoteReadError as error:
        raise AccountReadError(str(error)) from None
    except httpx.InvalidURL:
        raise AccountReadError("redirect_invalid_location") from None
    except (httpx.TransportError, TimeoutError):
        raise AccountReadError("network") from None
    if response.status_code == 401:
        raise AccountAuthExpired
    if 300 <= response.status_code < 400:
        raise AccountReadError(_classify_redirect(response.headers.get("location")))
    if response.status_code != 200:
        raise AccountReadError(f"http_{response.status_code}")
    content = response.content
    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if not 0 < len(content) <= _MAX_PDF_BYTES:
        raise AccountReadError("invalid_pdf_size")
    if media_type not in {"application/pdf", "application/octet-stream"}:
        raise AccountReadError("invalid_pdf_type")
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise AccountReadError("invalid_pdf")
    return content


def _write_pdf(folder: Path, name: str, content: bytes) -> bool:
    destination = folder / name
    if destination.is_symlink():
        raise AccountReadError("unsafe_storage")
    if destination.exists():
        if not destination.is_file() or destination.stat().st_size < 10:
            raise AccountReadError("unsafe_existing_file")
        with destination.open("rb") as existing:
            if not existing.read(5) == b"%PDF-":
                raise AccountReadError("unsafe_existing_file")
        return False

    handle, temporary = tempfile.mkstemp(prefix=".note-", suffix=".tmp", dir=folder)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(handle, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        # Avoid replacing an existing file/symlink if another request raced us.
        if destination.exists() or destination.is_symlink():
            raise AccountReadError("unsafe_existing_file")
        os.replace(temporary, destination)
        return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def download_contract_notes(
    account: AccountClient, from_date: date, to_date: date, dry_run: bool = False
) -> ContractNoteExport:
    if from_date > to_date or (to_date - from_date).days > 366:
        raise AccountReadError("invalid_date_range")
    rows, duplicate_transactions = await _scan_transactions(account, from_date, to_date)
    rows, next_date = _select_200_whole_days(rows)

    # Derive the URL identifiers ONLY from this authenticated broker response.
    refs: dict[tuple[str, str], date] = {}
    missing = 0
    duplicate_notes = 0
    for item in rows:
        if item.get("type") not in {"BUY", "SELL"}:
            raise AccountReadError("unexpected_transaction_type")
        owner = item.get("account")
        owner_id = owner.get("urlParameterId") if isinstance(owner, dict) else None
        note_id = item.get("noteId")
        if not isinstance(owner_id, str) or not isinstance(note_id, str):
            missing += 1
            continue
        if not _SAFE_ID.fullmatch(owner_id) or not _SAFE_ID.fullmatch(note_id):
            missing += 1
            continue
        key = (owner_id, note_id)
        if key in refs:
            duplicate_notes += 1
        else:
            refs[key] = _date_of(item)

    downloaded = already_present = failed = 0
    failure_codes: set[str] = set()
    hint = (
        r"%LOCALAPPDATA%\AvanzaMCP\contract-notes"
        if os.name == "nt"
        else "~/.local/share/avanza-mcp/contract-notes"
    )
    if not dry_run and refs:
        folder, hint = _note_directory()
        saved_bytes = 0
        for (owner_id, note_id), traded_on in refs.items():
            fingerprint = hashlib.sha256(
                f"{owner_id}:{note_id}".encode("ascii")
            ).hexdigest()[:24]
            filename = f"{traded_on:%Y%m%d}_{fingerprint}.pdf"
            destination = folder / filename
            if destination.exists() and not destination.is_symlink():
                try:
                    if _write_pdf(folder, filename, b""):
                        raise AssertionError("not reached")
                    already_present += 1
                except AccountReadError:
                    failed += 1
                    failure_codes.add("unsafe_existing_file")
                continue
            try:
                document = await _read_pdf(account, owner_id, note_id)
                if saved_bytes + len(document) > _MAX_EXPORT_BYTES:
                    failed += 1
                    failure_codes.add("export_size_limit")
                    continue
                if _write_pdf(folder, filename, document):
                    downloaded += 1
                    saved_bytes += len(document)
                else:
                    already_present += 1
            except AccountAuthExpired:
                raise
            except AccountReadError as error:
                failed += 1
                # Limited safe failure vocabulary, never any note/account IDs.
                code = str(error)
                if code.startswith("http_") and len(code) == 8 and code[5:].isdigit():
                    failure_codes.add(code)
                elif code in {
                    "network", "invalid_pdf_size", "invalid_pdf_type",
                    "invalid_pdf", "unsafe_storage", "unsafe_existing_file",
                    "invalid_note_reference", "request_not_allowed",
                    "redirect_missing_location", "redirect_login",
                    "redirect_insecure", "redirect_invalid_location",
                    "redirect_same_origin", "redirect_avanza_subdomain",
                    "redirect_external", "redirect_relative",
                    "redirect_limit", "redirect_loop", "redirect_unsupported_status",
                }:
                    failure_codes.add(code)
                else:
                    failure_codes.add("note_fetch_failed")

    return ContractNoteExport(
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        dry_run=dry_run,
        selected_transactions=len(rows),
        unique_notes=len(refs),
        downloaded=downloaded,
        already_present=already_present,
        missing_reference=missing,
        duplicate_transactions=duplicate_transactions,
        duplicate_notes=duplicate_notes,
        failed=failed,
        complete=(next_date is None and missing == 0 and failed == 0),
        next_from_date=next_date.isoformat() if next_date else None,
        storage_hint=hint,
        failure_codes=sorted(failure_codes),
    )
