"""Document-only routing policy shared by the worker HTTP and account clients."""
import re
from urllib.parse import unquote, urlsplit

import httpx

NOTE_ROUTE_RE = re.compile(
    r"^/_api/contract-notes/documents/[A-Za-z0-9_-]{1,128}/[A-Za-z0-9_-]{1,128}/note\.pdf$",
    re.ASCII,
)
MAX_PDF_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3


class NoteReadError(RuntimeError):
    """Sanitized document transport failure without URLs or session material."""


def resolve_redirect(current: httpx.URL, location: str | None) -> httpx.URL:
    """Resolve worker-private document URLs without trusting their authority."""
    if not location:
        raise NoteReadError("redirect_missing_location")
    if (
        len(location) > 8192
        or any(ord(char) <= 32 or ord(char) == 127 for char in location)
        or "\\" in location
        or location.startswith("//")
    ):
        raise NoteReadError("redirect_invalid_location")
    try:
        parsed = urlsplit(location)
        target = current.join(location)
        if parsed.fragment or "#" in location or target.userinfo:
            raise NoteReadError("redirect_invalid_location")
        if target.scheme != "https":
            raise NoteReadError("redirect_insecure")
        if target.host != "www.avanza.se" or target.port not in (None, 443):
            raise NoteReadError("redirect_external")
        path = unquote(target.path).lower()
        if any(term in path for term in (
            "/login", "/logout", "/auth", "/bankid", "/session", "/disconnect",
        )):
            raise NoteReadError("redirect_login")
        if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
            raise NoteReadError("redirect_invalid_location")
        return target
    except (ValueError, httpx.InvalidURL):
        raise NoteReadError("redirect_invalid_location") from None
