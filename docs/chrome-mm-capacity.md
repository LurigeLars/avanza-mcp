# Optional Chrome extension: MM köpbar SEK

The separate Manifest V3 extension in `chrome-extension/` shows **MM ask price ×
visible volume at that MM ask level**. It requires no investment amount, order
field changes or per-trade clicks. It is not account buying power, total depth
at all prices, an execution guarantee, or a commission-inclusive amount.

## Install

1. Update the normal runtime checkout to the merged `main` version and run
   `uv sync --locked`. Keep development worktrees separate from the runtime.
2. Open `chrome://extensions`, enable Developer mode and choose **Load unpacked**.
   Select this repository's `chrome-extension` directory. Keep that directory in
   the same location so Chrome's extension ID and pairing stay stable.
3. Reload any Avanza tabs already open. The extension observes existing requests
   from document start; an already-open stream cannot be recovered retroactively.
4. Enable the optional bridge in the **existing authenticated MCP process** with
   environment variable `AVANZA_CAPACITY_BRIDGE=1`, then restart that process at a
   convenient time. A memory-only Avanza session may require reconnecting through
   the existing MCP BankID flow after restart. Never enter BankID or account
   credentials in the extension.

   For the repository's Windows scheduled launcher, re-register it with its
   **current session mode** and the new switch, for example:

   ```powershell
   .\scripts\windows\install-public-http-task.ps1 -SessionMode memory_only -CapacityBridge
   ```

   The installer does not stop an existing server. Restart the existing task
   when ready; preserve its existing session policy. Do not start a second MCP
   process on the same port. The bridge reuses that process's client and isolated
   authenticated worker. No gateway or remote MCP configuration change is needed.

5. Generate a private, single-use pairing code locally:

   ```powershell
   uv run --locked python -m avanza_mcp.capacity_bridge pair
   ```

   Enter the code in the extension popup within ten minutes and choose **Para**.
   `PAIRED` confirms the token exchange. It does not establish fresh market data.
   Do not paste the code or permanent token into chat or GitHub.
6. Open Avanza's **Hitta certifikat** list. The added **MM köpbar SEK** column
   updates automatically while the tab is visible. Existing Avanza filters and
   spread sorting remain authoritative. This version adds no separate amount
   filter or client-side reordering of Angular-managed rows.
7. On `/handla/order.html/kop/<id>` or `/salj/<id>`, an information panel appears
   immediately below the visible order depth. The amount appears only when the
   MM ask marker, SEK currency, price, volume, instrument ID and a fresh snapshot
   from the page's own depth stream agree. This path needs neither pairing nor
   the local bridge. No extension requests are sent to Avanza from this page.

For an explicitly **public-only** independent process, use
`uv run --locked python -m avanza_mcp.capacity_bridge serve-public`. Do not run it
alongside the integrated bridge. Public data can be delayed; a quick HTTP 200
does not imply a fresh book, and delayed books display `STALE`.

## Freshness and limits

- `marketMakerLevelInAsk` is a **zero-based index**. Only that level's sell side
  is used. A missing, negative, malformed or out-of-range index never falls back
  to best ask, bid, last trade, a guessed MM price, or aggregated levels.
- Currency comes from identity-checked certificate metadata (cached five
  minutes). Only verified SEK is displayed; no implicit FX conversion.
- A positive finite price, positive integral volume and source `receivedTime`
  no more than **5 seconds old** are required. Future or missing source times
  fail closed. HTTP receive time, DOM mutation time and heartbeats cannot make
  an old book fresh. Both bridge and extension check the source clock.
- The bridge processes at most **5 unique numeric IDs per request**, 1 KiB
  request bodies, four application requests and eight HTTP connections at a
  time. Reads have a two-second per-instrument deadline and three-second batch
  deadline. The existing client's provider concurrency, pacing and cooldown
  remain in force.
- Up to **20 currently visible rows**, top first, are fetched progressively in
  batches of five. Extra visible rows explicitly show the limit. Scrolling
  brings other rows into scope. Hidden/offscreen rows cause no new fetches.
- Quote cache: **750 ms**, at most 200 entries, shared across tabs. Cached
  results are checked again against source age before returning. Duplicate
  concurrent reads coalesce. Metadata cache is also bounded to 200 entries.
- Refresh is one second after a completed visible-row pass; failure pauses
  requests for ten seconds. Rendering/freshness checks run every 250 ms and DOM
  changes update the order panel through a MutationObserver. Browser scheduling
  and multi-batch upstream latency still apply; this is not a one-second SLA.
- `STALE`, `OFFLINE`, `TIMEOUT`, `RATE_LIMITED`, `PAIR_REQUIRED`, `MM_MISSING`,
  `CURRENCY_UNKNOWN` and unverified DOM all withhold the amount. Hidden tabs
  pause fetching and clear their observations. Failed reads do not extend a
  previous quote's lifetime.

## Security and independence

The bridge binds only IPv4 `127.0.0.1:18762`. Fixed `/pair` and `/capacity` routes
accept JSON POSTs plus narrow OPTIONS preflights. There is no arbitrary URL,
upstream path, account, order or MCP-tool dispatch endpoint. Host validation
rejects DNS rebinding; ordinary website/null Origins are denied.

Pairing uses a random 80-bit one-time code, ten-minute expiry and at most five
exchange attempts per ten minutes. A successful exchange pins the exact Chrome
extension Origin and rotates a random 256-bit bearer token. Windows/macOS/Linux
native credential storage holds only hashes and the pinned Origin. The token
stays in `chrome.storage.local` restricted to `TRUSTED_CONTEXTS`; it is never
returned to content scripts. A global 120-request/minute bound, body limits,
timeouts and concurrency limits bound local work. Pairing-store failure closes
access rather than enabling an unauthenticated fallback.

The service worker validates the sender page and accepts only numeric IDs and
bounded batches. It has only the loopback host permission and omits cookies.
Content scripts run on the exact Avanza hosts to survive SPA navigation. The
MAIN-world observer only clones Avanza's **existing order-depth SSE response**;
it never reads request headers, cookies, security tokens, account streams or
order fields. Only allowlisted public depth scalars cross to the isolated
content script, where they are checked against the visible MM row and URL.
SSE frame buffering is capped at 64 KiB and 50 levels. Changed/unreadable
responses produce an unverified/stale panel, not guessed capacity.

Page DOM and MAIN-world messages are not cryptographically authenticated market
evidence. A compromised Avanza page or same-user malware can spoof page data;
local pairing cannot defend a compromised OS account. The bridge's authenticated
session remains inside the existing isolated worker.

Security patterns were adapted from the paired Trump Monitor bridge in
`LurigeLars/trade-alert`; this implementation imports and depends on none of it.
No new runtime dependencies or MCP tools were added. With the flag unset, MCP
behavior is unchanged. A port conflict logs bridge `OFFLINE` and leaves MCP
running. The embedded listener does not replace the owner's signal handlers.

Chrome platform references: [cross-origin requests and sender validation](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)
and [storage access levels](https://developer.chrome.com/docs/extensions/reference/api/storage).

## Validation

```powershell
uv run --locked pytest tests/unit -q
node --test tests/gateway/*.test.mjs tests/extension/*.test.cjs
uv run --with playwright==1.58.0 playwright install chromium
uv run --with playwright==1.58.0 python tests/browser/capacity_display.py
uv run --locked python scripts/benchmark_capacity_live.py <one-to-five-orderbook-ids>
```

The optional benchmark flag `--authenticated` asks a separate isolated persistent
worker to reuse an existing native-store session. Its `authenticated_responses`
counter distinguishes actual authenticated replies from public fallback. It
cannot attach to another process's memory-only session. It never logs accounts,
cookies or tokens. Run only a small, known instrument set.

The isolated browser test loads the real MV3 extension and reaches a real
loopback HTTP listener with synthetic pages/data. It covers pairing, list
identity (including product links), MM level 1 rather than best ask, source-time
gating, DOM updates, missing MM, stale expiry, actual bridge shutdown/network
refusal and unchanged order fields. The required `fork-ci` job runs these checks
with Playwright's pinned Chromium installation. Python tests cover mapping,
malformed money fields, currency, cache, source age, partial failures, pairing,
rotation, origin/Host/body/ID bounds and listener lifecycle.

### Measurements on 2026-11-13

- Isolated Chromium 151, 20 synthetic DOM updates: median **0.45 ms**, p95
  **0.80 ms**, max **1.10 ms**, measured from DOM change to panel update. This
  excludes upstream market delivery and is not a real trading-session SLA.
- Real Avanza client, one instrument: cold **479 ms**, five warm reads
  **130–150 ms**, median **137 ms**; cache **0.012 ms**.
- Real Avanza client, five verified certificate IDs: cold **1,372 ms**, five
  warm batches **714–791 ms**, median **750 ms**; cache **0.012 ms**. The requested
  isolated persistent worker returned **zero authenticated responses**: this was
  public fallback, with source books about **15–65 minutes old**. All amounts
  correctly remained withheld as `STALE`. These are HTTP/client timings, not
  evidence of executable live depth.
- The extension added 20 columns on the actual public Angular certificate list,
  with `PAIR_REQUIRED` before pairing. The user's existing logged-in list and
  order depth were visually inspected: links carry order-book identity and the
  sell-side MM price has the expected asterisk. The extension has **not** been
  installed in that user's normal Chrome profile; live logged-in SSE matching
  and sustained authenticated list freshness remain to be accepted there.

## Disable / rollback

Disable/remove the extension in Chrome. Unset `AVANZA_CAPACITY_BRIDGE` or
re-register the Windows launcher without `-CapacityBridge`, then restart the
existing MCP process when convenient. No orders, account settings, Avanza
filters or Trade Alert settings need changing. Re-pairing rotates any previous
bridge token. A code update does not require re-pairing when the extension ID
and native pairing record remain the same.
