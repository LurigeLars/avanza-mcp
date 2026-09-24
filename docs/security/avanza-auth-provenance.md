# Avanza authentication protocol provenance

Status: current provenance record for the authenticated read-only implementation.

## Current implementation source

The BankID implementation is selectively ported from `AnteWall/avanza-mcp`:
- feature commit: `e03009672e9c18e8575497768c2755c2fd20a279`
- CI/keyring fix: `4a26a8d4ff7b20f5878865780ac7221b5beb765b`
- upstream license: MIT

Relevant protocol endpoints:
- `POST /_api/authentication/v2/sessions/bankid`
- `POST /_api/authentication/v2/sessions/bankid/restart`
- `POST /_api/authentication/v2/sessions/bankid/collect`
- `POST /_api/authentication/v2/sessions/bankid/cancel`
- `GET /_api/authentication/session/info/session`
- `DELETE /_api/authentication/sessions/webtoken`

The implementation keeps an isolated cookie jar, verifies the resulting Avanza session, captures `X-SecurityToken`, and stores only the verified session material in the native OS credential store.

## Historical reference

The older `LurigeLars/avanza-market-scan` authentication path used Avanza username/password plus optional TOTP and was derived from `Qluxzz/avanza`. That historical flow remains useful protocol provenance but is not the credential model used here. In particular, this implementation does not accept or store an Avanza password or TOTP secret.

Independent historical corroboration also existed in `AnteWall/avanza-rs` and `Eitraz/avanza-api`. Those repositories are references only and are not runtime dependencies.

## Local modifications to upstream auth

This fork deliberately differs from upstream by:
- preserving the fork's FastMCP 4/public market contracts;
- disabling automatic BankID browser launch;
- failing closed instead of silently falling back to anonymous market data after auth expiry;
- adding authenticated-payload redaction;
- enforcing a fixed account HTTP method/path allowlist;
- hiding `get_credit_info`, `get_current_offers`, and `get_forum_posts` from the MCP catalog;
- retaining the existing public gateway as credential-free.

No real account credential or session material is committed to the repository or used in offline tests.
