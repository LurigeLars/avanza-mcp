"""Placera Forum authentication adapted to the isolated loopback BankID flow."""

from __future__ import annotations

import html
import json
import secrets
from string import Template

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from ..client.forum import ForumBankIDClient
from .browser import AuthState, AuthStatus, BrowserAuth

_FORUM_MESSAGES: dict[AuthState, str] = {
    "disconnected": "Placera Forum is not connected.",
    "awaiting_approval": "Approve Placera Forum sign-in in the local browser window.",
    "awaiting_disconnect": "Placera Forum disconnection is pending.",
    "starting": "Starting Placera Forum BankID authentication.",
    "scanning": "Scan the Placera Forum BankID QR code in the local browser window.",
    "connected": "Placera Forum is connected for this MCP process.",
    "idle": "Placera Forum session is idle.",
    "denied": "Placera Forum BankID authentication was cancelled or denied.",
    "timed_out": "Placera Forum BankID authentication timed out.",
    "error": "Placera Forum authentication failed safely.",
}

_FORUM_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Connect Placera Forum</title>
<style nonce="$nonce">
:root{--brand:#007a58;--ink:#10201b;--muted:#60706a;--line:#dce7e2;--surface:#fff;--canvas:#f2f7f5}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;font:15px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--canvas)}
.card{width:min(100%,520px);border:1px solid var(--line);border-radius:20px;background:var(--surface)}
main{padding:32px}h1{margin:0 0 8px;font-size:34px;line-height:1.12}.lead{margin:0;color:var(--muted);font-size:16px}
.stage{margin:28px 0 22px;padding:20px;border:1px solid var(--line);border-radius:16px;text-align:center}
#status{margin:0;font-weight:650}#helper{margin:5px 0 0;color:var(--muted);font-size:14px}
#qr{display:none;margin:18px auto 0;width:min(100%,290px);aspect-ratio:1;padding:12px;border:1px solid var(--line);border-radius:16px;background:#fff}
#qr.active{display:grid;place-items:center}#qr img{display:block;width:100%;height:auto}
.actions{display:flex;gap:10px}button{min-height:48px;border-radius:12px;padding:0 18px;border:0;font:inherit;font-weight:700;cursor:pointer}
#start{flex:1;color:#fff;background:var(--brand)}#cancel{color:var(--ink);background:#eef3f1}button:disabled{cursor:wait;opacity:.58}
.notes{margin:22px 0 0;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
@media(prefers-color-scheme:dark){:root{--ink:#eef8f4;--muted:#9aada5;--line:#2c3d36;--surface:#13211c;--canvas:#09110e}#cancel{background:#263731;color:var(--ink)}}
</style>
</head>
<body>
<article class="card"><main>
<h1 id="title">$title</h1>
<p class="lead" id="lead">$lead</p>
<section class="stage" aria-live="polite">
<p id="status" role="status">$status</p>
<p id="helper">$helper</p>
<div id="qr"></div>
</section>
<div id="actions" class="actions">
<button id="start" type="button">Start BankID</button>
<button id="cancel" type="button">Cancel</button>
</div>
<aside class="notes">
<p>This signs in only to Placera Forum. Your Avanza banking session is not sent to Placera Forum.</p>
<p>The forum token stays in an isolated local worker and is never returned through MCP tool results.</p>
</aside>
</main></article>
<script nonce="$nonce">
const csrf=$csrf,base=$path;
const status=document.getElementById('status'),helper=document.getElementById('helper');
const qr=document.getElementById('qr'),title=document.getElementById('title');
const lead=document.getElementById('lead'),actions=document.getElementById('actions');
const start=document.getElementById('start'),cancel=document.getElementById('cancel');
async function call(suffix,method='GET'){
 const r=await fetch(base+suffix,{method,headers:method==='POST'?{'X-CSRF-Token':csrf}:{}});
 if(!r.ok)throw new Error('request failed');return r.json();
}
function render(state){
 status.textContent=state.message;qr.innerHTML=state.qr_svg||'';qr.classList.toggle('active',!!state.qr_svg);
 start.disabled=['starting','scanning'].includes(state.state);
 start.hidden=['starting','scanning','connected'].includes(state.state);
 if(state.state==='scanning')helper.textContent='Open BankID on your phone and scan this QR code.';
 if(state.state==='connected'){title.textContent="You're connected";lead.textContent='Placera Forum posting is ready.';helper.textContent='You can close this window.';actions.hidden=true;}
 if(['denied','timed_out','error','disconnected'].includes(state.state)&&state.state!=='disconnected'){start.hidden=false;start.disabled=false;start.textContent='Try again';}
}
async function poll(){try{const s=await call('/status');render(s);if(['starting','scanning'].includes(s.state))setTimeout(poll,1400);}catch{status.textContent='Local authentication page lost connection.'}}
start.addEventListener('click',async()=>{start.disabled=true;try{render(await call('/start','POST'));setTimeout(poll,500);}catch{status.textContent='Could not start BankID.';start.disabled=false;}});
cancel.addEventListener('click',async()=>{try{render(await call('/cancel','POST'));}catch{}window.close();});
</script>
</body></html>"""
)


class ForumBrowserAuth(BrowserAuth):
    """BrowserAuth variant whose session contains only a Placera Forum bearer token."""

    def __init__(self) -> None:
        super().__init__(
            client_factory=ForumBankIDClient,
            qr_renderer=lambda markup: markup,
            store=None,
            poll_interval=1.5,
            attempt_timeout=120.0,
            session_idle_seconds=2 * 60 * 60,
        )

    def status(self) -> AuthStatus:
        message = _FORUM_MESSAGES[self._state]
        if self._error_code is not None:
            message = f"{message} Error code: {self._error_code}."
        return AuthStatus(
            state=self._state,
            message=message,
            error_code=self._error_code,
        )

    def _headers(self, *, nonce: str | None = None) -> dict[str, str]:
        script = f"'nonce-{nonce}'" if nonce is not None else "'none'"
        return {
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; form-action 'none'; "
                f"script-src {script}; style-src {script}; connect-src 'self'; "
                "img-src data:; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }

    async def _page(self, request: Request) -> Response:
        if not self._valid_host(request):
            return Response(status_code=400, headers=self._headers())
        nonce = secrets.token_urlsafe(18)
        page = _FORUM_PAGE.substitute(
            nonce=nonce,
            title=html.escape("Connect Placera Forum"),
            lead=html.escape("Sign in with BankID to enable explicit forum posting."),
            status=html.escape(_FORUM_MESSAGES[self._state]),
            helper=html.escape("The login is separate from your Avanza banking session."),
            csrf=json.dumps(self._csrf_token),
            path=json.dumps(f"/{self._path_token}"),
        )
        return HTMLResponse(page, headers=self._headers(nonce=nonce))
