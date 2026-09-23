# Avanza Authenticated Read-Only — Security Architecture

Status: implemented candidate on `feat/authenticated-readonly-v2`; independent review findings H1/M1-M3 were remediated in `acde1b24f0fe756c3d84fba4b002e4f79c4b0b0b`. Delta security rereview is required before any real BankID/live-account validation.

## Boundary

The default/public Avanza MCP remains credential-free. Authentication is opt-in with `AVANZA_MCP_AUTH=1` and starts a separate authenticated server composition. The authenticated process mounts the existing public tool contract, but authenticated market reuse is deliberately narrow: only stock quote, order-depth, and recent-trades requests may use the verified Avanza session. Other public-market tools continue anonymously.

Authentication uses Avanza's BankID web-session flow. The temporary consent/QR listener binds only to `127.0.0.1`; credentials are never accepted as MCP arguments. Verified cookies and `X-SecurityToken` are persisted only through the native OS credential store. On Windows this is Windows Credential Manager.

## Exposed authenticated tools

- session: `connect_avanza`, `disconnect_avanza`, `get_auth_status`
- portfolio: `get_accounts`, `get_holdings`, `get_transactions`, `get_portfolio_insights`
- saved data/research: `get_watchlists`, `get_price_alerts`, `get_instrument_news`, `get_insider_transactions`
- trading state, read-only: `get_active_orders`, `get_deals`, `get_stop_loss_orders`
- realtime-capable market reuse: existing stock `get_stock_quote`, `get_orderbook`, and `get_recent_trades`

Supported in the internal client but intentionally not exposed: `get_credit_info`, `get_current_offers`, `get_forum_posts`. Activate only after a concrete need and review.

## Hard security properties

- No order placement, modification, cancellation, transfer, withdrawal, or settings mutation is implemented.
- The account client enforces an explicit HTTP method/path allowlist before network execution.
- Startup session restoration is serialized and completes before the MCP surface accepts calls, preventing stale restore from racing a later explicit connect/disconnect.
- Authenticated market requests are permitted only for three exact stock path shapes: quote, orderdepth, and trades.
- Those authenticated market responses use strict allowlist projections; unknown top-level or nested fields are discarded instead of relying on a credential-field blacklist.
- Authenticated HTTP error bodies are never inspected, logged, or inserted into raised errors.
- Auth expiry fails closed; authenticated requests never silently retry anonymously and return delayed data as if it were authenticated.
- Disconnect clears cached authenticated HTTP client state immediately. If Avanza logout cannot be confirmed, local state remains disconnected but reports `revocation_unconfirmed`.
- Account outputs use explicit Pydantic projections with `extra="forbid"`; raw authenticated account responses are not returned.
- Session material has redacted representations and sanitized errors.
- The public gateway keeps its independent public-tool allowlist and does not expose account tools.
- BankID UI never starts automatically on server startup or auth expiry; `connect_avanza` is an explicit user action.
- Upstream Avanza text is treated as untrusted data, never as instructions.

## Out of scope

Trading writes are YAGNI. If needed later, build them as a separately reviewed capability with an explicit human authorization boundary; do not extend the present read-only request allowlist.

## Verification gates

Completed offline on the remediation branch:

- full unit suite;
- public gateway tests;
- package build;
- BankID protocol and native-keyring failure tests;
- public/auth surface tests;
- auth-expiry fail-closed tests;
- account request-allowlist negative tests;
- restore/disconnect concurrency regression test;
- strict authenticated quote/order-depth projection tests;
- authenticated error-body leakage regression test;
- authenticated-client cleanup regression test.

Before any real BankID session is used:

1. Obtain an independent delta security verdict for the exact remediated head SHA.
2. If that verdict is `SECURITY_PASS` or `PASS_WITH_CONSTRAINTS`, run the bounded local BankID test with the owner present.
3. Verify session persistence/deletion behavior in Windows Credential Manager.
4. Compare authenticated vs anonymous quote/order-book freshness and `isRealTime` behavior during market hours.
5. Verify account/holding/order/deal/stop-loss projections against live responses without printing raw private payloads.
6. Verify disconnect and record whether remote revocation was confirmed.
7. Keep remote authenticated ChatGPT exposure disabled pending a separate integration review.
