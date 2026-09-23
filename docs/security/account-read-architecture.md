# Avanza Authenticated Read-Only — Security Architecture

Status: implemented candidate on `feat/authenticated-readonly-v2`; offline verification complete, real BankID/live-account validation still required before remote enablement.

## Boundary

The default/public Avanza MCP remains credential-free. Authentication is opt-in with `AVANZA_MCP_AUTH=1` and starts a separate authenticated server composition. The authenticated process mounts the existing market tools so a verified Avanza session can be reused for realtime-capable market requests, and adds a bounded read-only account/activity surface.

Authentication uses Avanza's BankID web-session flow. The temporary consent/QR listener binds only to `127.0.0.1`; credentials are never accepted as MCP arguments. Verified cookies and `X-SecurityToken` are persisted only through the native OS credential store. On Windows this is Windows Credential Manager.

## Exposed authenticated tools

- session: `connect_avanza`, `disconnect_avanza`, `get_auth_status`
- portfolio: `get_accounts`, `get_holdings`, `get_transactions`, `get_portfolio_insights`
- saved data/research: `get_watchlists`, `get_price_alerts`, `get_instrument_news`, `get_insider_transactions`
- trading state, read-only: `get_active_orders`, `get_deals`, `get_stop_loss_orders`

Supported in the internal client but intentionally not exposed: `get_credit_info`, `get_current_offers`, `get_forum_posts`. This keeps the model tool catalog and attack surface smaller; activate only after a concrete need and review.

## Hard security properties

- No order placement, modification, cancellation, transfer, withdrawal, or settings mutation is implemented.
- The account client enforces an explicit HTTP method/path allowlist before network execution.
- Authenticated market requests fail closed on expired authentication; they never silently retry anonymously and return delayed data as if it were authenticated.
- Flexible authenticated market payloads are recursively stripped of credential/identity-like fields before model validation/output.
- Account outputs use explicit Pydantic projections with `extra="forbid"`; raw authenticated responses are not returned.
- Session material has redacted representations and sanitized errors.
- The public gateway keeps its independent public-tool allowlist and does not expose account tools.
- BankID UI never starts automatically on server startup or auth expiry; `connect_avanza` is an explicit user action.

## Out of scope

Trading writes are YAGNI. If needed later, build them as a separately reviewed capability with an explicit human authorization boundary; do not extend the present read-only request allowlist.

## Validation gates

Completed offline: Python 3.12/3.13 unit tests, gateway tests, package build, BankID protocol tests, native-keyring failure tests, public/auth surface tests, auth-expiry fail-closed tests, secret-redaction tests, and account request-allowlist negative tests.

Before remote authenticated access is enabled:
1. Run bounded local BankID login with the owner present.
2. Verify session persistence/revocation in Windows Credential Manager.
3. Compare authenticated vs anonymous quote/order-book freshness and `isRealTime` behavior during market hours.
4. Verify account/holding/order/deal/stop-loss projections against live responses.
5. Review the exact remote gateway allowlist; authenticated account tools must not be added to the existing public connector by accident.
