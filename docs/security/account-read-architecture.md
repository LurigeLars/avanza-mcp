# Avanza Authenticated Read-Only — Security Architecture

Status: implemented candidate on `feat/authenticated-readonly-v2`. Two independent security reviews have been completed. The second review closed H1/M1/M2 but kept M3 open; M3-A/M3-B and the associated startup/transport hardening were remediated in `786836c7e1bf16182850cc610fce9b831fab1976`. A final delta rereview of the exact current head is required before any real BankID/live-account validation.

## Boundary

The default/public Avanza MCP remains credential-free. Authentication is opt-in with `AVANZA_MCP_AUTH=1` and starts a separate authenticated server composition.

Authenticated market reuse is deliberately narrow: only stock quote, order-depth, and recent-trades requests may use the verified Avanza session. Other public-market tools continue anonymously.

Authentication uses Avanza's BankID web-session flow. The temporary consent/QR listener binds only to `127.0.0.1`; credentials are never accepted as MCP arguments. Verified cookies and `X-SecurityToken` are persisted only through the native OS credential store. On Windows this is Windows Credential Manager.

## Exposed authenticated tools

- session: `connect_avanza`, `disconnect_avanza`, `get_auth_status`
- portfolio: `get_accounts`, `get_holdings`, `get_transactions`, `get_portfolio_insights`
- saved data/research: `get_watchlists`, `get_price_alerts`, `get_instrument_news`, `get_insider_transactions`
- trading state, read-only: `get_active_orders`, `get_deals`, `get_stop_loss_orders`
- realtime-capable market reuse: existing stock `get_stock_quote`, `get_orderbook`, and `get_recent_trades`

Supported in the internal client but intentionally not exposed: `get_credit_info`, `get_current_offers`, `get_forum_posts`.

## Hard security properties

- No order placement, modification, cancellation, transfer, withdrawal, or settings mutation is implemented.
- The account client enforces an explicit HTTP method/path allowlist before network execution.
- Startup session restoration completes before the MCP surface accepts calls.
- Authenticated market requests are permitted only for three exact stock path shapes: quote, orderdepth, and trades.
- Those authenticated market responses use strict allowlist projections; unknown top-level and nested fields are discarded.
- Authenticated HTTP error bodies are never parsed, logged, or inserted into raised errors.
- Auth expiry fails closed; authenticated requests never silently retry anonymously.
- Authentication client selection and the authenticated network request share the same client lock. Disconnect/clear therefore waits for an already-started authenticated request, while requests queued after the provider-visible session is cleared cannot recreate a client from stale session state.
- Session rotation closes the previous authenticated HTTP client rather than retaining it.
- `request_authenticated()` accepts only relative same-origin paths; absolute, protocol-relative, query-bearing, and fragment paths are rejected before network execution.
- Fresh-client logout restores the saved session cookies and security token before the Avanza logout request.
- HTTP 401 is not treated as proof of successful remote revocation. Failure to confirm logout is surfaced as `revocation_unconfirmed` while local access remains removed.
- Disconnect deletes the local persisted session and clears cached authenticated HTTP state.
- Account outputs use explicit Pydantic projections with `extra="forbid"`; raw authenticated account responses are not returned.
- Session material has redacted representations and sanitized errors.
- Process-global authenticated request wiring is reset even if startup restore itself raises or is cancelled.
- The public gateway keeps its independent public-tool allowlist and does not expose account tools.
- BankID UI never starts automatically on server startup or auth expiry; `connect_avanza` is an explicit user action.
- Upstream Avanza text is treated as untrusted data, never as instructions.

## Out of scope

Trading writes are YAGNI. If needed later, build them as a separately reviewed capability with an explicit human authorization boundary; do not extend the present read-only request allowlist.

Remote authenticated ChatGPT/Cloudflare exposure is also out of scope for the current live-test gate.

## Security review history

Initial review of `bf4d8883e5fd1c790ac099accf444dec43fd7376`:
- verdict: `NEEDS_REVIEW`
- H1: startup restore race
- M1: authenticated market payload leakage risk
- M2: authenticated error-body leakage
- M3: logout/session cleanup semantics

First remediation: `acde1b24f0fe756c3d84fba4b002e4f79c4b0b0b`.

Second delta review of `e9f907e35db9cc6d647a0d89e0c94aee6659393f`:
- H1: CLOSED
- M1: CLOSED
- M2: CLOSED
- M3: OPEN
  - M3-A: fresh logout client did not restore saved cookies and accepted 401 as confirmed revocation
  - M3-B: concurrent request could recreate an authenticated client from a stale session after disconnect
- additional hardening requested for startup cleanup and generic authenticated transport destination constraints

Second remediation: `786836c7e1bf16182850cc610fce9b831fab1976`.

## Offline validation after second remediation

The remediation harness completed successfully before committing the code:

- full Python unit suite;
- public gateway tests;
- package build;
- fresh-client logout cookie/token regression;
- logout-401-not-confirmed regression;
- concurrent disconnect/request stale-session regression;
- relative same-origin authenticated transport regression;
- startup restore failure/global wiring cleanup regression.

The ordinary PR CI/CodeQL checks must also pass on the final documentation head before the next independent rereview is treated as the live-test gate.

## Next gate

Before any real BankID/session material is used:

1. Obtain an independent delta security rereview for the exact current PR head.
2. Require `SECURITY_PASS` or `PASS_WITH_CONSTRAINTS`.
3. Only then run a bounded local stdio/loopback BankID test with the owner present.
4. Keep Cloudflare/remote authenticated exposure disabled.
5. During live validation, do not print raw account payloads, cookies, `X-SecurityToken`, QR/auth tokens, or authenticated response headers/bodies.
6. Verify Windows Credential Manager persistence/deletion, connect/restore/disconnect, remote revocation semantics, account projections, and authenticated quote/orderdepth/trades freshness.
