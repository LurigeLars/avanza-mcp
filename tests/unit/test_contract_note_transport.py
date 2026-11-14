"""Synthetic document redirects exercise the real isolated HTTP transport."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from avanza_mcp.client.accounts import AccountAuthExpired, AccountClient, AccountReadError
from avanza_mcp.client.bankid import SessionMaterial
from avanza_mcp.client.base import AvanzaClient
from avanza_mcp.client.contract_notes import _MAX_PDF_BYTES, _read_pdf

_ORIGIN = "https://www.avanza.se"
_PATH = "/_api/contract-notes/documents/synthetic-owner/synthetic-note/note.pdf"
_PDF = b"%PDF-1.4\nsynthetic document\n%%EOF\n"


@asynccontextmanager
async def _client(**kwargs):
    cookies = httpx.Cookies()
    cookies.set("synthetic-session", "private-cookie", domain="www.avanza.se", path="/")
    state = [SessionMaterial(tuple(cookies.jar), "private-token")]
    async with AvanzaClient(
        session_provider=lambda: state[0], min_request_interval=0, request_jitter=0,
        **kwargs,
    ) as client:
        yield client, state


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", [
    "/document.pdf?signature=a%2Fb+z&key=one&key=two",
    "../../../../../document.pdf?signature=a%2Fb+z&key=one&key=two",
    "https://www.avanza.se/document.pdf?signature=a%2Fb+z&key=one&key=two",
])
async def test_document_chain_preserves_raw_query_and_session(respx_mock, status, location):
    respx_mock.get(_ORIGIN + _PATH).respond(status, headers={"location": location})
    final = respx_mock.get(
        _ORIGIN + "/document.pdf?signature=a%2Fb+z&key=one&key=two"
    ).respond(200, content=_PDF, headers={"content-type": "application/pdf"})
    async with _client() as (client, _):
        document = await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
        assert document == _PDF
        assert client._public_client().cookies.get("synthetic-session") is None
        assert "x-securitytoken" not in client._public_client().headers
    request = final.calls.last.request
    assert request.url.query == b"signature=a%2Fb+z&key=one&key=two"
    assert request.headers["x-securitytoken"] == "private-token"
    assert request.headers["cookie"] == "synthetic-session=private-cookie"
    assert "referer" not in request.headers
    assert all(call.request.method == "GET" for call in respx_mock.calls)


@pytest.mark.parametrize("location,code", [
    (None, "redirect_missing_location"),
    ("https://evil.example/note?secret=private-token", "redirect_external"),
    ("https://documents.avanza.se/note", "redirect_external"),
    ("https://www.avanza.se.evil.example/note", "redirect_external"),
    ("https://www.avanza.se:444/note", "redirect_external"),
    ("http://www.avanza.se/note", "redirect_insecure"),
    ("//www.avanza.se/note", "redirect_invalid_location"),
    ("https://user:password@www.avanza.se/note", "redirect_invalid_location"),
    ("/note#private-token", "redirect_invalid_location"),
    ("/note\\evil", "redirect_invalid_location"),
    (" /note", "redirect_invalid_location"),
    ("/note\t?secret=private-token", "network"),
    ("https://[invalid/note", "redirect_invalid_location"),
    ("/note?" + "x" * 8192, "redirect_invalid_location"),
    ("/login?secret=private-token", "redirect_login"),
    ("/logout", "redirect_login"),
    ("/%61uth/session", "redirect_login"),
])
async def test_unsafe_redirect_never_sends_second_request(respx_mock, location, code):
    headers = {} if location is None else {"location": location}
    respx_mock.get(_ORIGIN + _PATH).respond(302, headers=headers)
    async with _client() as (client, _):
        with pytest.raises(AccountReadError, match=f"^{code}$") as caught:
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
    assert "private-token" not in str(caught.value)
    assert len(respx_mock.calls) == 1


async def test_second_hop_is_checked_before_credentials_can_leave_origin(respx_mock):
    respx_mock.get(_ORIGIN + _PATH).respond(302, headers={"location": "/first.pdf"})
    respx_mock.get(_ORIGIN + "/first.pdf").respond(
        302, headers={"location": "https://evil.example/note?secret=private-token"},
    )
    async with _client() as (client, _):
        with pytest.raises(AccountReadError, match="^redirect_external$"):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
    assert len(respx_mock.calls) == 2


async def test_three_hops_succeed_and_clear_waits_for_the_chain(respx_mock):
    respx_mock.get(_ORIGIN + _PATH).respond(302, headers={"location": "/1.pdf"})
    respx_mock.get(_ORIGIN + "/1.pdf").respond(302, headers={"location": "/2.pdf"})
    respx_mock.get(_ORIGIN + "/2.pdf").respond(302, headers={"location": "/3.pdf"})
    started, finish = asyncio.Event(), asyncio.Event()

    async def final(request):
        started.set()
        await finish.wait()
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})

    respx_mock.get(_ORIGIN + "/3.pdf").mock(side_effect=final)
    async with _client() as (client, _):
        read = asyncio.create_task(_read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note"))
        await asyncio.wait_for(started.wait(), 5)
        clear = asyncio.create_task(client.clear_authenticated_session())
        await asyncio.sleep(0)
        assert not clear.done()
        finish.set()
        assert await read == _PDF
        await clear
        assert client._authenticated_client is None
    assert len(respx_mock.calls) == 4


@pytest.mark.parametrize("loop", [True, False])
async def test_chain_has_cycle_detection_and_three_hop_limit(respx_mock, loop):
    respx_mock.get(_ORIGIN + _PATH).respond(302, headers={"location": "/1.pdf"})
    for number in range(1, 4):
        respx_mock.get(f"{_ORIGIN}/{number}.pdf").respond(
            302, headers={"location": "/1.pdf" if loop else f"/{number + 1}.pdf"},
        )
    async with _client() as (client, _):
        with pytest.raises(AccountReadError, match="^redirect_loop$" if loop else "^redirect_limit$"):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
    assert len(respx_mock.calls) == (2 if loop else 4)


@pytest.mark.parametrize("status", [300, 304, 305, 306])
async def test_other_3xx_are_not_followed(respx_mock, status):
    respx_mock.get(_ORIGIN + _PATH).respond(status, headers={"location": "/note.pdf"})
    async with _client() as (client, _):
        with pytest.raises(AccountReadError, match="^redirect_unsupported_status$"):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
    assert len(respx_mock.calls) == 1


async def test_expired_session_on_redirect_is_propagated(respx_mock):
    respx_mock.get(_ORIGIN + _PATH).respond(302, headers={"location": "/note.pdf"})
    respx_mock.get(_ORIGIN + "/note.pdf").respond(401)
    async with _client() as (client, _):
        with pytest.raises(AccountAuthExpired):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")


async def test_session_change_does_not_attach_new_token_mid_chain(respx_mock):
    async with _client() as (client, state):
        def replace_session(request):
            state[0] = SessionMaterial((), "different-token")
            return httpx.Response(302, headers={"location": "/note.pdf"})
        respx_mock.get(_ORIGIN + _PATH).mock(side_effect=replace_session)
        with pytest.raises(AccountAuthExpired):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
    assert len(respx_mock.calls) == 1


class _Chunks(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(10):
            self.reads += 1
            yield b"x" * (_MAX_PDF_BYTES // 2)

    async def aclose(self):
        self.closed = True


async def test_pdf_body_stops_at_byte_budget_and_closes_stream(respx_mock):
    stream = _Chunks()
    respx_mock.get(_ORIGIN + _PATH).mock(return_value=httpx.Response(
        200, stream=stream, headers={"content-type": "application/pdf"},
    ))
    async with _client() as (client, _):
        with pytest.raises(AccountReadError, match="^invalid_pdf_size$"):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
    assert stream.reads == 3 and stream.closed


async def test_entire_chain_has_one_deadline(respx_mock):
    async def stalled(request):
        await asyncio.sleep(1)
        return httpx.Response(200, content=_PDF)
    respx_mock.get(_ORIGIN + _PATH).mock(side_effect=stalled)
    async with _client(request_timeout=0.01) as (client, _):
        with pytest.raises(AccountReadError, match="^network$"):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")


@pytest.mark.parametrize("path", ["/logout", "//evil.example/note", _PATH + "?token=secret"])
async def test_document_transport_has_fixed_entry_route(respx_mock, path):
    async with _client() as (client, _):
        with pytest.raises(AccountReadError, match="^request_not_allowed$"):
            await client.request_authenticated_note(path)
    assert len(respx_mock.calls) == 0


async def test_document_transport_never_uses_public_or_delegate_fallback(respx_mock):
    async def delegate(*args):
        raise AssertionError("document metadata must not leave the worker")

    async with AvanzaClient(authenticated_request_delegate=delegate) as client:
        with pytest.raises(AccountAuthExpired):
            await _read_pdf(AccountClient(client), "synthetic-owner", "synthetic-note")
    assert len(respx_mock.calls) == 0
