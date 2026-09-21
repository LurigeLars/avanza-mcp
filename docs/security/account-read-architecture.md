# Avanza Account Read — Security Architecture

Status: design only. No account authentication or private-account code is implemented on this branch.

## Objective

Add future read-only account access without weakening the existing public-market-data MCP boundary.

The design must assume that account credentials and authenticated sessions are highly sensitive even when the account capability is read-only.

## Non-negotiable security requirements

1. **No plaintext account credentials at rest.**
   - Never store username, password, recovery data, session secrets or equivalent authentication material in Git, `.env`, JSON/YAML/TOML config, shell history, batch files, logs, test fixtures, crash reports or temporary files.
   - Do not accept account credentials as MCP tool arguments.
   - Do not pass account credentials through Cloudflare, the public gateway, ChatGPT, Codex or Claude Code.

2. **Use an OS-protected secret store.**
   - On Windows, credentials must be stored using an operating-system protected facility such as Windows Credential Manager / DPAPI-backed storage.
   - The implementation must not invent its own encryption format or persist an application-managed decryption key beside encrypted data.
   - Secret retrieval must occur only inside the local account process.

3. **Separate trust boundary from public market data.**
   - Existing public Avanza MCP remains read-only public market data.
   - Account access runs as a separate local process/profile, proposed loopback endpoint: `127.0.0.1:8768/mcp`.
   - Account tools are not added to the existing public 34-tool allowlist.
   - No order, transfer, withdrawal, settings-change or other write capability is permitted in the account-read process.

4. **Fail closed.**
   - If the OS secret store is unavailable, authentication fails.
   - If a requested operation is not on the explicit account-read allowlist, deny it.
   - Do not fall back to environment variables, plaintext files, command-line credentials or interactive credential prompts through MCP.

5. **Minimize session exposure.**
   - Prefer short-lived authenticated sessions.
   - Keep session material in process memory where feasible.
   - If session persistence is required, store it only through the same OS-protected secret mechanism and document the exact lifetime/revocation behavior.
   - Never log cookies, bearer tokens, session IDs, authentication responses or full request/response headers.

6. **Redact observability by construction.**
   - Structured logs may contain tool name, duration, success/failure class and non-sensitive request IDs.
   - Logs must not contain credentials, session material, portfolio payloads, account numbers, personal identifiers or raw authenticated responses.
   - Error handling must map sensitive upstream errors to sanitized internal error classes.

7. **Private data is separate from authentication secrets.**
   - Even with local credential storage, account tool results can contain sensitive portfolio/account data.
   - Remote exposure to ChatGPT must therefore be a separate explicit activation step after security review.
   - Local-only operation is the default during development and validation.

## Proposed topology

```text
Public market data
------------------
ChatGPT / local agents
        |
existing public gateway / local HTTP
        |
Avanza public MCP :8767
        |
public Avanza endpoints


Private account read
--------------------
local client initially
        |
Avanza Account Read MCP :8768
        |
account-read service boundary
        |---- OS-protected secret store
        |
authenticated Avanza session
        |
explicit read-only account endpoints
```

The account service must not share credential state with the public MCP process.

## Tool metadata

Public market-data tools:

```text
readOnlyHint: true
destructiveHint: false
openWorldHint: true
```

Private account-read tools are expected to be:

```text
readOnlyHint: true
destructiveHint: false
openWorldHint: false
```

Metadata is descriptive only; enforcement must exist in code and tests.

## Future remote access

Remote ChatGPT access is not part of the initial account implementation.

If later approved, use a separate Cloudflare Access application/AUD and preferably a separate hostname/gateway policy from the public market-data connector. The remote account surface must expose only explicit account-read tools.

## Explicitly out of scope

- Order placement
- Order modification/cancellation
- Money transfers or withdrawals
- Account/profile/settings changes
- Credential reset/recovery automation
- Storing or automating a second factor or BankID secret
- Reusing account credentials in test fixtures
- Sending credentials to any model or remote MCP client

Any future write or trading capability requires a separate trust boundary, separate security review and explicit human authorization before activation.

## Implementation gates

Before account authentication code is allowed to move beyond design:

1. Verify the provider authentication flow and supported session lifecycle without exposing real credentials.
2. Select and threat-model the OS secret-storage mechanism.
3. Define the exact read-only endpoint/tool allowlist.
4. Add secret-redaction and negative tests before live-account testing.
5. Run an independent security review.
6. Perform bounded local-only live validation.
7. Only after successful review may remote account access be designed or enabled.

## Testing requirements

At minimum:

- credential values never appear in process arguments, environment, logs or MCP messages;
- unapproved account operations are rejected before network execution;
- secret-store failure is fail-closed;
- logs are verified free of secrets and private payloads;
- session invalidation/revocation is tested;
- process restart behavior does not create plaintext persistence;
- public gateway cannot enumerate or call account-read tools;
- public MCP continues functioning without access to account credentials.

