# Avanza Authenticated Read-Only — Security Architecture

## Scope

The MCP surface is read-only, but the underlying Avanza web session is **not assumed to be read-only**. A stolen cookie/security-token set may have broader authority outside this MCP. Treat Avanza session material as a high-value banking credential.

The public market-data MCP remains credential-free. Authenticated account access is available only through the reviewed account/session tools and the narrowly approved realtime stock paths.

## Process boundary

The long-lived FastMCP/control-plane process must not hold reusable Avanza cookies or the security token.

```text
ChatGPT / local MCP client
        |
        v
long-lived FastMCP control plane
(no Avanza session material)
        |
        +--> public anonymous Avanza requests
        |
        +--> isolated auth worker
               |
               +--> OS credential store (persistent mode only)
               +--> Avanza authenticated HTTPS
```

Authenticated account results return through the worker IPC channel. Session cookies, security tokens, BankID QR material and raw authenticated headers never do.

Worker commands are operation names plus bounded tool arguments. No generic arbitrary authenticated URL/method tool is exposed.

## Session modes

### persistent (default)

The verified session is stored in the native OS credential store. On Windows this is Windows Credential Manager. Each authenticated account operation starts a fresh worker process: load stored session, validate it, perform one approved operation, close the authenticated HTTP client, then exit.

The long-lived MCP process never receives the session. Realtime-capable stock quote, order-depth and trade requests use the same short-lived worker pattern.

### memory_only

No reusable Avanza session is written to the OS credential store. BankID is performed inside a dedicated isolated worker. Successful authenticated account operations reset a 15-minute idle timer; public market-data calls do not.

After 15 minutes without an authenticated account operation the worker sends Avanza remote logout, clears local HTTP/session state, and exits. A process/server/computer restart therefore requires BankID again.

### one_shot

No reusable session is persisted. After BankID, the isolated worker permits one explicit authenticated account workflow. When that workflow completes, the worker performs remote logout and exits. If unused, it logs out after five minutes.

Public market-data calls remain anonymous in one-shot mode so a quote lookup cannot accidentally consume the one authenticated workflow.

## Account request allowlist

The model-facing MCP surface exposes session tools, portfolio/account reads, saved-data/research reads, and read-only trading-state reads. Internal client implementations for `get_credit_info`, `get_current_offers`, and `get_forum_posts` remain unexposed.

No order placement, modification, cancellation, transfer, withdrawal or settings mutation is implemented.

## Authenticated market reuse

Only exact GET paths for stock `quote`, `orderdepth`, and `trades` may use an authenticated worker. Authenticated responses are projected onto reviewed public-market fields before they leave the worker boundary. In `one_shot` mode these requests remain anonymous.

## Disconnect semantics

Explicit disconnect uses the local confirmation page. Persistent mode loads and validates the saved session inside an isolated worker, removes the OS credential, and sends Avanza remote logout. A 401 from the logout endpoint is treated as an already-invalid remote session; network/other failures can still be reported as `revocation_unconfirmed` after local credential removal.

Memory-only and one-shot modes contain no persistent credential. Explicit disconnect or automatic expiry sends remote logout from the credential-holding worker before it exits.

## Hard security properties

- Reusable session material is absent from the long-lived MCP/control-plane process.
- Worker subprocess arguments and environment do not contain the Avanza session.
- Worker stdout uses a restricted JSON protocol and credential-shaped result keys are rejected.
- Authenticated account operations are explicit operation names, not arbitrary requests.
- Account outputs use strict Pydantic projections with `extra="forbid"`.
- Authenticated upstream error bodies are not surfaced through MCP.
- Session validation occurs inside the worker before persistent-session use.
- Auth expiry fails closed for authenticated requests.
- The OS credential store is used only in `persistent` mode.
- Memory-only/one-shot session material disappears when the isolated worker exits.
- The Cloudflare/Docker gateway never receives Avanza cookies or security tokens.
- BankID approval remains a temporary loopback-only browser flow.

## Threat-model limitation

The read-only MCP allowlist protects against model/tool misuse. It does **not** reduce the authority of a stolen Avanza web session outside the MCP. If an attacker compromises the Windows user/process deeply enough to read the active worker's memory or the persistent credential store, the session must be treated as potentially reusable with Avanza directly.

The isolation modes reduce exposure time and persistence; they do not turn an Avanza web session into a server-side read-only token.
