# Authenticated Avanza read-only architecture

Status: implementation candidate. Real Avanza credentials/session material are **not activated by this change**.

## Provenance

The BankID/session implementation is selectively ported from `AnteWall/avanza-mcp`:
- auth feature commit: `e03009672e9c18e8575497768c2755c2fd20a279`
- follow-up CI/keyring fix: `4a26a8d4ff7b20f5878865780ac7221b5beb765b`

The fork keeps its own public/options/leveraged contracts instead of importing upstream's broader public-model rewrite.

## Hard boundary

- Public FastMCP remains credential-free on `127.0.0.1:8767`.
- Authenticated FastMCP is a separate process on `127.0.0.1:8768`.
- BankID login uses a temporary loopback browser listener.
- Verified session cookies and `X-SecurityToken` are persisted only in the native OS credential store.
- The Cloudflare gateway never receives Avanza cookies or the security token; those are attached only by the host-side Avanza client.
- Authenticated market payloads pass through a recursive redaction layer for token/cookie/password and customer/account identity fields before flexible public-market models can expose them.
- A 401 from an authenticated market request invalidates the session and fails closed. It is never silently retried anonymously.
- Background startup and auth expiry never open BankID UI. Reauthentication requires an explicit `connect_avanza` call.
- No order placement, modification, cancellation, money movement, or other trading write capability is implemented.

## Exposed authenticated tools

Session:
- `connect_avanza`
- `disconnect_avanza`
- `get_auth_status`

Portfolio/account:
- `get_accounts`
- `get_holdings`
- `get_transactions`
- `get_portfolio_insights`

Read-only trading state:
- `get_active_orders`
- `get_deals`
- `get_stop_loss_orders`

Saved/research:
- `get_watchlists`
- `get_price_alerts`
- `get_instrument_news`
- `get_insider_transactions`

The existing public market tools are also mounted and may reuse the authenticated session. Authentication alone is not proof of realtime data; returned realtime/delay/freshness fields must be verified empirically.

## Intentionally not exposed

Upstream implements these, but this fork deliberately removes them from the authenticated MCP tool provider:
- `get_credit_info`
- `get_current_offers`
- `get_forum_posts`

They should be reconsidered only when a concrete workflow justifies the additional model context, privacy surface, or prompt-injection risk.

## Remote deployment

Use a separate Cloudflare Access application, hostname/tunnel, and AUD from the public MCP.

Flow:

```text
ChatGPT
  -> Cloudflare Access (authenticated Avanza app)
  -> dedicated Cloudflare tunnel
  -> authenticated gateway with explicit tool allowlist
  -> host 127.0.0.1:8768
  -> Avanza authenticated session in Windows Credential Manager
```

Do not point the public gateway/AUD at port 8768.

## Activation gate

Synthetic/mock tests and CI may run without credentials. Before real BankID/session material is used:
1. review the actual PR/diff and CI/security checks;
2. obtain an independent security verdict under the global external-code/MCP policy;
3. obtain explicit human approval after that verdict;
4. only then install/start the authenticated task and complete BankID.

Realtime REST behavior and any future push/streaming feed remain runtime-validation items; they are not assumed from successful authentication alone.
