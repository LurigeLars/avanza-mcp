# Ponytail review: optional MM capacity extension

This change displays visible MM ask capacity on Avanza's certificate list and
order page. The list uses a paired read-only loopback route backed by the existing
Avanza client; the order page reads the displayed MM row and passively observes
the page's existing SSE stream for identity and source time.

Scope reviewed: extension manifest, MAIN/isolated script boundary, message sender
validation, trusted token storage, native pairing store, bounded HTTP routes,
capacity calculation/cache, authenticated server lifespan, Windows launcher,
acceptance tests, benchmark and installation/rollback instructions. Existing
client pacing, request projection and isolated broker delegates were traced.
Expected usage is one local user, a small number of visible browser tabs and at
most twenty visible rows per pass.

## Findings corrected before submission

1. **Product identity on logged-out lists.** Real public list rows have a
   certificate detail link but no buy/sell link. Identifying rows only through
   order URLs would leave the column empty. The parser now accepts the exact
   certificate-detail path too, while excluding the underlying stock link.
   Pure-parser, browser-fixture and actual public-page checks cover this case.

2. **Embedded listener lifecycle.** A second Uvicorn listener must not take
   ownership of the MCP process's signal handlers. The embedded server leaves
   signal capture to its owner. A pre-bound loopback socket turns port conflicts
   into a visible bridge-OFFLINE warning while preserving MCP availability.
   Tests cover shutdown handlers, port conflict and enabled/disabled composition
   with the existing client.

3. **Streaming frame boundaries.** Splitting a CRLF pair between network chunks
   could prevent parsing a valid snapshot. Normalization now occurs after
   appending each chunk. A test splits every character, verifies the untouched
   original response, proves no extra request and checks that an unrelated
   credential-shaped field never leaves the MAIN observer.

4. **Cold-list fan-out.** Twenty simultaneous metadata-plus-depth reads compete
   with the existing client's pacing and can exhaust the freshness budget.
   Progressive batches of five show the upper rows first. Quote and metadata
   caches, body/ID limits, a request semaphore and deadlines bound the work.
   No provider concurrency or cooldown protection was weakened.

5. **Recycled/partial list rows.** If Avanza replaces a row's links before its
   content is complete, the previous injected cell must not retain an amount.
   Missing or conflicting identity now clears the cell, with a browser regression
   check. The extension cache is pruned on failed as well as successful passes.

6. **Display deadlines versus shared authentication.** The broker deliberately
   retires an IPC worker on cancellation to avoid consuming a mismatched late
   response. Cancelling a slow bridge read would therefore discard a memory-only
   Avanza session. Display deadlines now shield the underlying read, coalesce
   pending IDs, and allow at most five pending tasks; other IDs return `BUSY`.
   Tests prove both timeout and request cancellation leave the underlying read
   alive, that the pending bound holds, and that a late result remains usable
   only with its original source timestamp. The owner's existing request
   deadline still bounds slow upstream work; shared MCP read contention remains
   a documented limit.

No unresolved correctness/security findings were identified in the reviewed
implementation. Missing/negative/wrong-type MM indexes, missing currency,
stale/future source timestamps, malformed price/volume, upstream errors,
unauthorized Origins/Host values and oversized requests all withhold amounts.
No order actions, account reads or Trade Alert runtime dependency were added.

## Evidence and acceptance limit

Local validation included the complete Python unit suite (726 tests at that
stage), all gateway/extension JavaScript tests (27), followed by focused tests
for the final lifecycle, timeout, partial-failure and opt-in changes. The build
passed. The required fork CI also runs the complete final unit suite, extension
tests and isolated real-MV3/real-loopback browser acceptance test.

The isolated browser verifies MM level 1, visible DOM parsing, pairing,
source-time gating, stale/offline behavior and unchanged order fields. The
actual public Avanza Angular list was tested with the extension; the user's
logged-in list and order book were visually inspected without order actions.
See [measurements and installation](chrome-mm-capacity.md) for timing evidence.

Verdict: Ship the opt-in implementation through normal CI/PR gates. Do not call
authenticated live behavior accepted until the extension is loaded in the normal
Chrome session and fresh source snapshots match there.

Not checked: sustained trading-session load, actual fills, a long-run latency
SLA, or future Avanza UI/API compatibility. No separate sorting/filtering system,
new runtime dependency, scheduler, account export or order feature was built.
