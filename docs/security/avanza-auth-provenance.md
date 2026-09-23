# Avanza authentication protocol provenance

Status: design/provenance record for the future read-only account capability. No account credentials or authenticated account implementation are added by this document.

## Historical implementation in our code

The existing authenticated Avanza implementation lives in `LurigeLars/avanza-market-scan`:

- file: `src/avanza_scan/execution.py`
- introduction commit: `05654fe6e0569416cad781be316bf5271b1f9687`
- current inspected repository head: `c9d38995d81b5134439243d469a8ca7312077519`

That implementation uses the following protocol:

1. `POST /_api/authentication/sessions/usercredentials`
2. request body containing `maxInactiveMinutes`, `username`, and `password`
3. if Avanza returns a `twoFactorLogin`, support only method `TOTP`
4. optional `POST /_api/authentication/sessions/totp`
5. obtain `X-SecurityToken` from the successful authentication response
6. preserve HTTP session/cookie state in the client
7. use the authenticated session only for read-only market-data/order-book GET requests

The historical scanner code reads credentials from process environment variables. That storage/loading model is explicitly **not** accepted for the future account-read implementation.

## What the Git history does and does not prove

The introduction commit documents the safety goal and authenticated execution-data behavior, but it does **not** record:

- a browser/DevTools capture of Avanza login;
- a crawl of the Avanza login page;
- a specific upstream repository as the source of the authentication protocol;
- copied third-party source attribution for the auth implementation.

The Git history alone therefore does not establish the original source.

The project owner has since explicitly confirmed that, during the earlier ChatGPT work that produced the scanner authentication flow, `Qluxzz/avanza` was the repository consulted for the Avanza login implementation. Treat that as owner-attested provenance even though the original commit omitted attribution.

Accordingly:

> The scanner authentication flow was derived from the Avanza login protocol implemented in `Qluxzz/avanza`, then adapted into the scanner's async read-only execution source. The repository history failed to record that provenance at the time.

## Primary current protocol reference

Primary comparison reference:

- repository: `Qluxzz/avanza`
- inspected commit: `a6a18a948f88cb7e340051e480b203b2ee917eed`
- relevant files: `avanza/avanza.py`, `avanza/constants.py`
- license: MIT, copyright André Andersson

The current Qluxzz implementation independently confirms:

- `/_api/authentication/sessions/usercredentials`
- username/password + `maxInactiveMinutes`
- successful login without a second factor when `twoFactorLogin` is absent
- optional TOTP flow at `/_api/authentication/sessions/totp`
- `X-SecurityToken`
- persistent HTTP session/cookie state
- authenticated account-read endpoints including:
  - `GET /_api/account-overview/overview/categorizedAccounts`
  - `GET /_api/position-data/positions`

Those account endpoints are **candidate protocol references only** until verified against the current Avanza service with bounded read-only testing.

## Independent corroborating references

Two additional independent implementations were checked:

### `AnteWall/avanza-rs`

- inspected commit: `5ba8f80709278008831cd5cc066b327c457cd1b9`
- relevant file: `src/client.rs`
- license: MIT, copyright Ante Wall

It corroborates the username/password endpoint, TOTP endpoint and `X-SecurityToken` flow.

### `Eitraz/avanza-api`

- inspected commit: `2a302c059315724128abd604ae4593c4270b6de2`
- relevant file: `TotpAuthentication.java`
- license: MIT, copyright Petter Alstermark

It independently corroborates the same usercredentials/TOTP protocol and authenticated headers/session behavior.

These repositories are protocol references, not planned runtime dependencies.

## Comparison: historical scanner vs current Qluxzz implementation

| Area | Historical `avanza-market-scan` | `Qluxzz/avanza` | Account-read decision |
| --- | --- | --- | --- |
| HTTP client | async `httpx.AsyncClient` | synchronous `requests.Session` | Keep async/local minimal client |
| Username/password endpoint | same usercredentials endpoint | same | Reuse protocol semantics |
| No-2FA response | supported in auth function | supported | Username/password is baseline |
| Credential model | constructor currently requires TOTP secret/code | supports credential variants | Do not require TOTP before server asks |
| TOTP | local RFC6238 or supplied code | supported | Fail closed if server requires unsupported factor; do not persist 2FA secret |
| Session state | httpx cookie jar + `X-SecurityToken` | requests cookie jar + token/body fields | Keep all session material local |
| Inactivity | 30 minutes | current default 24 hours | Prefer conservative 30-minute session |
| Credential source | environment variables | application credential object | Replace with Windows OS-protected secret store |
| Account reads | none | account overview + positions available | Add only explicit reviewed GET allowlist |
| Write surface | none after login | library also contains trading/watchlist writes | Do **not** use full library as runtime dependency |
| Error handling | sanitized `ExecutionSourceError` | library exceptions | Preserve sanitized/fail-closed errors |
| Live verification | code-ready, not accepted against live account | external library behavior | Bounded live read-only validation required |

## Dependency decision

Do **not** add `Qluxzz/avanza`, `avanza-rs`, or another full Avanza client as a runtime dependency for account-read.

Reason:

- the account-read requirement is very small;
- the current Qluxzz package includes write/trading capabilities outside our scope;
- a full dependency would increase capability and supply-chain surface;
- the existing code already demonstrates the minimal HTTP/session pattern we need.

Use the external implementations as pinned protocol references only. Implement the smallest read-only account client inside the account trust boundary.

If any substantial third-party source code is later copied rather than independently implemented, preserve the applicable MIT copyright/license notice and document the exact source commit and copied scope.

## Security requirements carried forward

The new account implementation must differ from the historical scanner in one critical respect:

- **No username/password/session secret in environment variables or plaintext files.**

Credentials must be retrieved inside the local account process from a Windows OS-protected secret store. Username, password, cookies, `X-SecurityToken`, authentication-session identifiers and second-factor material must never be:

- MCP arguments or results;
- sent through Cloudflare;
- sent to ChatGPT/Codex/Claude;
- written to logs;
- stored in Git;
- placed in command-line arguments;
- stored in plaintext config or `.env`.

Read-only portfolio/account data may be returned through approved MCP tools.

## Validation plan before implementation acceptance

1. Implement the credential-store abstraction with fake/test secrets only.
2. Implement authentication from the pinned protocol reference without importing a write-capable Avanza library.
3. Unit-test username/password-only success, server-requested second factor, bad auth, missing token and secret redaction.
4. Add an explicit network-method/path allowlist; authenticated account client must not execute arbitrary paths or non-approved methods.
5. Start with candidate GET endpoints for categorized accounts and positions.
6. Perform bounded local live validation only after real credentials are available.
7. Confirm that current Avanza responses/session behavior match the protocol references.
8. Only then expose the reviewed account-read tools through their separate MCP/gateway boundary.

