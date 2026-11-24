# Avanza Order Extension — optional Chrome extension

The separate Manifest V3 extension in `chrome-extension/` shows **MM ask price ×
visible volume at that MM ask level**. It requires no investment amount, order
field changes or per-trade clicks. It is not account buying power, total depth
at all prices, an execution guarantee, or a commission-inclusive amount.

Version **0.1.17** removes the extra one-second browser delay between serial
batches. The next 250 ms rendering tick may start the next batch after the
previous response, subject to the existing bridge and provider pacing and
shared cooldowns. No server configuration or permissions changed.

Version **0.1.16** removes the twenty-visible-row limit. Newly visible
instruments are queued in serial batches of at most five, with a one-second
gap after each response. On-visible mode does not repeat successful reads
until the next view; it reads new scroll rows and allows one bounded retry
per instrument after transient failures. An isolated depth failure does not
stop other rows. A shared provider limit or unavailable bridge pauses the
queue; a second consecutive failure stops recovery until the next view or
scheduled refresh. Retained values are dimmed and their tooltip identifies
them as not live. Known HTTP error codes are preserved from a strict allowlist.
Provider and bridge limits are unchanged.

Version **0.1.15** validates the current pathname supplied by the isolated
content script after same-document navigation, rather than relying on Chrome’s
potentially stale sender pathname. The HTTPS Avanza top-frame check and the
read-only bridge, ID, pairing and freshness limits are unchanged. Reload the
extension and existing Avanza tabs to activate the new scripts.

Version **0.1.14** starts a new tab view with a dash while the first capacity
read is pending, including while the order-depth DOM is still loading. A
completed read or validation failure can then display FAILED. Previous-view
errors and late replies do not become failures for the new view.

Version **0.1.12** adds an English popup with a persistent **ON/OFF** switch
and toolbar badge. OFF removes the extension's amounts and stops new bridge
reads; pairing remains saved. Re-enabling starts a fresh current-view read.
Pairing controls collapse after setup and remain available under **Pair bridge again**.

**Refresh capacity** offers **When the tab is shown**, **Every 30 seconds**,
**Every minute** (default), **Every 2 minutes** and **Every 5 minutes**.
Settings apply to open tabs immediately and survive popup/worker restarts.
**When the tab is shown** reads on opening, returning to the tab or restoring a
page, with no recurring successful reads while staying there. New visible rows are
read when scrolling, and a transient failure gets a bounded retry. Provider
cooldowns still apply. Avanza's own
live stream continues normally and can update the order-page amount immediately.
All extension labels and tooltips are English; Avanza's Swedish site and SEK
number formatting are unchanged. Reload the extension and open Avanza tabs once
to install this version's scripts.

Version **0.1.11** waits one minute between completed list passes and order-page
bridge fallback reads. First reads and returning to a visible tab remain immediate,
subject to the shared cooldown. Passive page-stream updates remain immediate.
The fifteen-second gate for accepting new source data is unchanged.

Version **0.1.10** keeps the last verified amount visible during background
refresh without showing STALE. It is current-view memory only, cleared on
navigation, reload, hidden tabs and page restoration; no quote is saved for a
later visit. A failed read/validation displays FAILED until successful recovery.
The tooltip keeps the original source time. A new amount still requires the
existing fifteen-second source-age validation.

All tabs share a thirty-second retry pause after TIMEOUT/BUSY or connection
failure, and sixty seconds after RATE_LIMITED. Affected list passes stop early.
The retry deadline, settings and pairing persist across worker restarts; quote data does not.

Version **0.1.9** accepts the order stream's ISO `updated` timestamp as well as
the legacy numeric `receivedTime`. Both preserve the original source time and
the fifteen-second age limit; missing, invalid and future times stay unusable.

Version **0.1.7** refreshes immediately when a tab becomes visible again,
clears previous bridge data after a hard message-channel failure, and accepts
multiple small SSE events in a large network chunk. A fresh page stream takes
priority over a failed older bridge request. New source data still requires the
fifteen-second source-age gate. Missing MM quotes
leave the amount field blank. Reload the extension and Avanza tabs to update;
pairing survives at the same extension path/ID.

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

   Enter the code in the extension popup within ten minutes and choose **Pair**.
   **Paired** confirms the token exchange. It does not establish fresh market data.
   Do not paste the code or permanent token into chat or GitHub.
6. Open Avanza's **Hitta certifikat** list. The added **Max buy capacity** column
   updates automatically while the tab is visible. Existing Avanza filters and
   spread sorting remain authoritative. This version adds no separate amount
   filter or client-side reordering of Angular-managed rows.
7. On `/handla/order.html/kop/<id>` or `/salj/<id>`, the **Max buy capacity in visible
   order depth** panel appears immediately below the visible order depth. The amount appears only when the
   MM ask marker, SEK currency, price, volume, instrument ID and a fresh snapshot
   agree. The page's own depth stream is preferred and needs no pairing. If its
   source time is unavailable or no longer fresh, the paired local bridge can
   verify **only this URL's instrument**. That fallback adds a bounded read of
   the same book through the existing authenticated client. Price, volume and MM
   index must match the visible DOM; a mismatch displays FAILED, with the reason
   in its tooltip. Last trade is never a substitute for current ask depth.

For an explicitly **public-only** independent process, use
`uv run --locked python -m avanza_mcp.capacity_bridge serve-public`. Do not run it
alongside the integrated bridge. Public data can be delayed; a quick HTTP 200
does not imply a fresh book. Delayed books cannot establish a new verified amount.

## Freshness and limits

- `marketMakerLevelInAsk` is a **zero-based index**. Only that level's sell side
  is used. A missing, negative, malformed or out-of-range index never falls back
  to best ask, bid, last trade, a guessed MM price, or aggregated levels.
- Currency comes from identity-checked certificate metadata (cached five
  minutes). Only verified SEK is displayed; no implicit FX conversion.
- A positive finite price, positive integral volume and source `receivedTime`
  no more than **15 seconds old** are required to accept a new amount. Future or missing source times
  fail closed. HTTP receive time, DOM mutation time and heartbeats cannot make
  an old book fresh. Both bridge and extension check the source clock.
- The bridge processes at most **5 unique numeric IDs per request**, 1 KiB
  request bodies, four application requests and eight HTTP connections at a
  time. Display responses have a 2.5-second deadline and
  three-second batch deadline. At most five shielded reads may remain pending;
  repeated IDs reuse that task and further work returns `BUSY`. The existing
  client's provider concurrency, pacing, request deadlines and cooldown remain
  in force. A display timeout does **not** cancel shared broker IPC, which would
  otherwise retire a memory-only session. A slow shared upstream operation can
  still delay other MCP reads until its existing deadline; the extension stays
  fail-closed meanwhile. Pending tasks are cancelled only on owner shutdown.
- The integrated bridge uses **one authenticated worker batch** per group of
  up to five books, with concurrent HTTP reads through the existing 50 ms-paced
  client. Each depth HTTP read has a 1.5-second deadline **inside** the isolated
  worker. A slow instrument yields a per-item unavailable result rather than
  blocking the other books indefinitely. No delayed anonymous fallback is used
  in the integrated bridge; missing authentication displays `AUTH_REQUIRED`.
- All currently visible rows are eligible for progressive batches of at most
  five, with the next batch eligible on the next rendering tick (250 ms).
  Existing bridge pacing and cooldowns still apply. Newly visible rows are queued
  ahead of failed-row retries. Hidden/offscreen rows cause no new fetches.
- Version **0.1.4** pauses a row for **30 seconds** after `MM_MISSING`, or when
  the list has no usable ask and a probe returns stale/unavailable depth. The
  amount cell is blank during this pause. A changed list ask resumes reads at
  the next scheduled pass; interval mode also probes again after the pause
  (on-visible mode waits for the next view).
  Fresh verified broker depth takes priority over a missing list price and is
  refreshed normally. `STALE` alone never means the market maker is absent.
  Pauses clear on navigation/visibility changes and are bounded to 200 IDs.
- Quote cache: **750 ms**, at most 200 entries, shared across tabs. Cached
  results are checked again against source age before returning. Duplicate
  concurrent reads coalesce. Metadata cache is also bounded to 200 entries.
- Refresh defaults to **sixty seconds after a completed visible-row pass**,
  configurable in the popup since **0.1.12**. The on-visible option has no timer.
  Per-instrument failures do not abort later rows. Order-page bridge
  fallback uses the same selected interval; passive DOM/stream updates
  remain immediate.
  Whole-bridge failures, all-item timeouts or busy results pause requests across tabs for
  thirty seconds; rate limits pause them for sixty seconds. Missing authentication
  or pairing retains the selected retry interval. Provider `Retry-After` is also respected by
  the broker. Rendering/freshness checks run every 250 ms. The list geometry
  is cached and invalidated on relevant DOM changes, scrolling or resize;
  DOM changes update the order panel through a MutationObserver. Returning to a
  visible tab or restoring a page triggers an immediate read; existing bridge
  limits and provider cooldown still apply. Browser scheduling
  and multi-batch upstream latency still apply; this is not an exact sixty-second
  per-instrument SLA.
- STALE or a pending retry keeps the current view's last verified amount, even
  beyond fifteen seconds. Its original source time is never renewed. With no
  verified value yet, a dash is shown. The backend's source-age validation is
  unchanged; retained display history is not a fresh quote and is dimmed.
- OFFLINE, TIMEOUT, BUSY, DEPTH_UNAVAILABLE, AUTH_REQUIRED, RATE_LIMITED,
  PAIR_REQUIRED, CURRENCY_UNKNOWN and unverified DOM display FAILED and discard
  the retained amount. A pending retry does not clear that failure. Missing MM
  or ask prices still leave the cell blank. Hidden tabs stop fetching and clear
  observations, display history and errors. Navigation/reload/page restoration
  also clear them; reused rows never inherit another instrument's value.

## Security and independence

The bridge binds only IPv4 `127.0.0.1:18762`. Fixed `/pair` and `/capacity` routes
accept JSON POSTs plus narrow OPTIONS preflights. There is no arbitrary URL,
upstream path, account, order or MCP-tool dispatch endpoint. Host validation
rejects DNS rebinding; ordinary website/null Origins are denied.

Pairing uses a random 80-bit one-time code, ten-minute expiry and at most five
exchange attempts per ten minutes. A successful exchange pins the exact Chrome
extension Origin and rotates a random 256-bit bearer token. The configured `keyring`
backend holds only hashes and the pinned Origin; use a native operating-system
backend. An alternate configured backend is not guaranteed to provide native
credential-store protection. The token stays in `chrome.storage.local` restricted to `TRUSTED_CONTEXTS`; it is never
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
Each SSE event and the unfinished-frame buffer are capped at 64 KiB; up to
50 levels are accepted per event. Coalesced network chunks can contain multiple
valid events without triggering the per-event limit. Changed/unreadable
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

## Rate-limit boundary

The local 120-request/minute bound is **not** a verified Avanza quota. Each
accepted bridge request can fetch five books: the theoretical ceiling is 600
depth HTTP requests/minute before cache/pauses, plus cold metadata reads.
Avanza's own page traffic and other MCP reads are additional. The broker
respects depth HTTP 429 / Retry-After after a limit response, but this does not
guarantee avoidance of an initial limit. The 30-second per-row pause removes
unproductive reads; it is not a provider-wide upstream request budget.

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
loopback HTTP listener on an ephemeral test port with synthetic pages/data; it
does not stop or replace a live bridge. It covers pairing, list
identity (including product links), early stop/backoff after an overloaded batch,
MM level 1 rather than best ask, source-time gating, DOM updates, missing MM,
current-view retention and clearing, FAILED recovery, order-page bridge fallback,
exact DOM/bridge match, passive-stream
priority, actual bridge shutdown/network refusal and unchanged order fields.
The required `fork-ci` job runs these checks
on Ubuntu 24.04 with Playwright's pinned Chromium installation. Python tests cover mapping,
malformed money fields, currency, cache, source age, partial failures, pairing,
rotation, origin/Host/body/ID bounds and listener lifecycle.

### Measuring latency safely

The browser acceptance test reports synthetic DOM-to-panel processing times.
Those measurements exclude Avanza delivery, authentication and upstream reads.
The optional benchmark reports HTTP/client timings and source ages separately;
fast responses alone do not establish fresh depth. Keep machine-specific timing
reports, live-session observations and screenshots local. Do not commit browser
profiles, pairing material, account identifiers or deployment details.

## Disable / rollback

Disable/remove the extension in Chrome. Remove `-CapacityBridge` from the
Windows launcher startup and ensure `AVANZA_CAPACITY_BRIDGE` is unset or not `1`
in its environment, then restart the existing MCP process when convenient.
No orders, account settings, Avanza filters or Trade Alert settings need changing. Re-pairing rotates any previous
bridge token. A code update does not require re-pairing when the extension ID
and native pairing record remain the same.
