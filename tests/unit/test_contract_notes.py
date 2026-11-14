"""Contract-note exports use only synthetic authenticated broker responses."""
from datetime import date
import hashlib

import httpx
import pytest

from avanza_mcp.client.accounts import AccountAuthExpired, AccountReadError
from avanza_mcp.client.contract_notes import download_contract_notes, _classify_redirect
from avanza_mcp.models.account import ContractNoteExport


def _tx(number: int, day: str, *, note: str | None = None, owner: str = "test-owner"):
    return {
        "id": f"tx-{number}",
        "date": day,
        "type": "BUY" if number % 2 else "SELL",
        "account": {"id": "private", "urlParameterId": owner},
        "noteId": f"note-{number}" if note is None else note,
    }


_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}

    async def request_authenticated(self, method, path):
        self.calls.append((method, path))
        status, media_type, body = self.responses.get(
            path, (200, "application/pdf", _PDF)
        )
        return httpx.Response(status, headers={"content-type": media_type}, content=body)


class FakeAccount:
    def __init__(self, rows, *, count_padding=0):
        self.rows = rows
        self.count_padding = count_padding
        self._client = FakeTransport()
        self.calls = []

    async def _get(self, path, **kwargs):
        assert path == "/_api/transactions/list"
        params = kwargs["params"]
        assert params["transactionTypes"] == "BUY,SELL"
        assert params["maxElements"] == 201
        self.calls.append(params)
        selected = [
            row for row in self.rows
            if params["from"] <= row["date"][:10] <= params["to"]
        ]
        return {
            "transactions": selected[:201],
            "transactionsAfterFiltering": len(selected) + self.count_padding,
        }


@pytest.fixture
def safe_folder(tmp_path, monkeypatch):
    # Never write to the caller's actual user data directory in tests.
    def _folder():
        tmp_path.mkdir(parents=True, exist_ok=True)
        return tmp_path, "test-private-folder"

    monkeypatch.setattr("avanza_mcp.client.contract_notes._note_directory", _folder)
    return tmp_path


@pytest.mark.asyncio
async def test_downloads_private_pdfs_and_reuses_existing_files(safe_folder):
    client = FakeAccount([_tx(1, "2026-11-10"), _tx(2, "2026-11-10")])
    first = await download_contract_notes(
        client, date(2026, 11, 10), date(2026, 11, 10)
    )
    assert isinstance(first, ContractNoteExport)
    assert first.complete and first.downloaded == 2
    assert first.already_present == 0
    assert first.storage_hint == "test-private-folder"
    assert len(list(safe_folder.glob("*.pdf"))) == 2
    exposed = first.model_dump_json()
    assert "test-owner" not in exposed and "note-1" not in exposed
    assert "private" in exposed  # only the fixed synthetic folder hint

    second = await download_contract_notes(
        client, date(2026, 11, 10), date(2026, 11, 10)
    )
    assert second.downloaded == 0 and second.already_present == 2
    assert len(client._client.calls) == 2


@pytest.mark.asyncio
async def test_dry_run_is_read_only_and_does_not_create_folder(safe_folder):
    client = FakeAccount([_tx(1, "2026-10-10")])
    result = await download_contract_notes(
        client, date(2026, 10, 10), date(2026, 10, 10), dry_run=True
    )
    assert result.dry_run and result.complete and result.unique_notes == 1
    assert result.downloaded == 0 and not client._client.calls
    assert not list(safe_folder.glob("*.pdf"))


@pytest.mark.asyncio
async def test_bisects_and_returns_resume_date_at_200_boundary(safe_folder):
    rows = [
        *(_tx(i, "2026-11-10") for i in range(1, 201)),
        _tx(201, "2026-11-11"),
    ]
    client = FakeAccount(rows)
    result = await download_contract_notes(
        client, date(2026, 11, 10), date(2026, 11, 11), dry_run=True
    )
    assert result.unique_notes == 200 and result.selected_transactions == 200
    assert result.complete is False and result.next_from_date == "2026-11-11"
    assert len(client.calls) == 3
    resumed = await download_contract_notes(
        client, date(2026, 11, 11), date(2026, 11, 11), dry_run=True
    )
    assert resumed.complete and resumed.unique_notes == 1


@pytest.mark.asyncio
async def test_fails_closed_when_one_day_exceeds_200(safe_folder):
    client = FakeAccount([_tx(i, "2026-11-10") for i in range(1, 202)])
    with pytest.raises(AccountReadError, match="single_day_exceeds_200"):
        await download_contract_notes(client, date(2026, 11, 10), date(2026, 11, 10))
    assert not client._client.calls


@pytest.mark.asyncio
async def test_deduplicates_transactions_and_notes(safe_folder):
    same_note = [_tx(1, "2026-11-10"), _tx(2, "2026-11-10", note="note-1")]
    client = FakeAccount(same_note + [same_note[0]])
    result = await download_contract_notes(
        client, date(2026, 11, 10), date(2026, 11, 10)
    )
    assert result.selected_transactions == 2
    assert result.duplicate_transactions == 1
    assert result.duplicate_notes == 1
    assert result.unique_notes == 1 and result.downloaded == 1


@pytest.mark.asyncio
async def test_missing_reference_is_reported_incomplete_without_ids(safe_folder):
    row = _tx(1, "2026-10-01")
    del row["noteId"]
    result = await download_contract_notes(
        FakeAccount([row]), date(2026, 10, 1), date(2026, 10, 1)
    )
    assert not result.complete
    assert result.missing_reference == 1
    assert result.downloaded == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,media_type,body,code",
    [
        (404, "text/html", b"not found", "http_404"),
        (200, "text/html", b"<html>login</html>", "invalid_pdf_type"),
        (200, "application/pdf", b"%PDF-no trailer", "invalid_pdf"),
        (200, "application/pdf", b"x" * (2 * 1024 * 1024 + 1), "invalid_pdf_size"),
    ],
)
async def test_pdf_failure_is_bounded(safe_folder, status, media_type, body, code):
    client = FakeAccount([_tx(1, "2026-11-10")])
    url = "/_api/contract-notes/documents/test-owner/note-1/note.pdf"
    client._client.responses[url] = status, media_type, body
    result = await download_contract_notes(
        client, date(2026, 11, 10), date(2026, 11, 10)
    )
    assert not result.complete and result.failed == 1
    assert result.failure_codes == [code]
    assert not list(safe_folder.glob("*.pdf"))


@pytest.mark.parametrize("location,classification", [
    (None, "redirect_missing_location"),
    ("https://www.avanza.se/_api/files?secret=do-not-expose", "redirect_same_origin"),
    ("https://documents.avanza.se/signed.pdf?secret=do-not-expose", "redirect_avanza_subdomain"),
    ("https://example.net/attachment?secret=do-not-expose", "redirect_external"),
    ("http://www.avanza.se/file", "redirect_insecure"),
    ("/login?state=do-not-expose", "redirect_login"),
    ("/_api/contract-notes/xyz.pdf?token=do-not-expose", "redirect_relative"),
])
def test_redirect_diagnostic_discloses_category_not_target(location, classification):
    result = _classify_redirect(location)
    assert result == classification
    assert "do-not-expose" not in result


@pytest.mark.asyncio
async def test_auth_expiry_propagates_without_saving(safe_folder):
    client = FakeAccount([_tx(1, "2026-11-10")])
    client._client.responses[
        "/_api/contract-notes/documents/test-owner/note-1/note.pdf"
    ] = (401, "text/html", b"expired")
    with pytest.raises(AccountAuthExpired):
        await download_contract_notes(client, date(2026, 11, 10), date(2026, 11, 10))
    assert not list(safe_folder.glob("*.pdf"))


@pytest.mark.asyncio
async def test_malicious_document_reference_does_not_make_request(safe_folder):
    client = FakeAccount([_tx(1, "2026-11-10", note="../private")])
    result = await download_contract_notes(
        client, date(2026, 11, 10), date(2026, 11, 10)
    )
    assert result.missing_reference == 1 and result.complete is False
    assert not client._client.calls


@pytest.mark.asyncio
async def test_existing_symlink_is_not_overwritten(safe_folder):
    owner_id, note_id = "test-owner", "note-1"
    fingerprint = hashlib.sha256(f"{owner_id}:{note_id}".encode("ascii")).hexdigest()[:24]
    external = safe_folder / "external.txt"
    external.write_text("do not overwrite")
    destination = safe_folder / f"20261110_{fingerprint}.pdf"
    try:
        destination.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    client = FakeAccount([_tx(1, "2026-11-10")])
    result = await download_contract_notes(
        client, date(2026, 11, 10), date(2026, 11, 10)
    )
    assert result.failed == 1 and not result.complete
    assert external.read_text() == "do not overwrite"


@pytest.mark.asyncio
async def test_date_validation_before_network():
    client = FakeAccount([])
    with pytest.raises(AccountReadError, match="invalid_date_range"):
        await download_contract_notes(client, date(2026, 11, 11), date(2026, 11, 10))
    assert not client.calls
