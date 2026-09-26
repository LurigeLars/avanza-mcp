# Remote authenticated Avanza MCP

## Scope

Remote ChatGPT access may call the reviewed authenticated read-only MCP tools through the existing Cloudflare Access gateway. The Avanza session credential itself remains on the Windows host.

```text
ChatGPT
  -> Cloudflare Access / Managed OAuth
  -> Docker gateway
  -> 127.0.0.1:8767 FastMCP control plane
       -> public anonymous market requests
       -> isolated auth worker
            -> Windows Credential Manager (persistent mode only)
            -> Avanza
```

## Credential boundary

The long-lived FastMCP process and the Docker/Cloudflare gateway do not receive Avanza cookies, the Avanza security token or BankID transaction/QR material.

For authenticated operations the FastMCP control plane sends only a restricted operation name and bounded tool arguments to a local worker process. The worker returns the reviewed account/market result, never session material. The broker rejects worker responses containing credential-shaped keys.

## Session modes

`AVANZA_SESSION_MODE` selects the credential lifetime:

- `persistent` (default): native OS credential store; fresh worker per authenticated operation.
- `memory_only`: no persistent credential; isolated worker session; remote logout and worker exit after 15 minutes without an authenticated account operation.
- `one_shot`: no persistent credential; one authenticated account workflow, then remote logout and worker exit; five-minute unused timeout.

The Windows task installer exposes the same selection with `-SessionMode`.

## Cloudflare boundary

The gateway uses the `@authenticated` allowlist profile when remote account tools are enabled. Cloudflare Access remains the external identity boundary. The gateway accepts only `/mcp`, validates the configured Access identity, rate-limits it, strips inbound credential/forwarding headers, filters `tools/list`, blocks non-allowlisted `tools/call` requests, and compacts schemas/results.

Account result data requested by the user necessarily traverses the encrypted MCP/Cloudflare/ChatGPT path. Avanza session credentials do not.

## BankID boundary

`connect_avanza` may be invoked remotely, but the approval and BankID QR page is opened and served on the Windows host over a temporary `127.0.0.1` listener. QR/auth transaction material is not returned through MCP.

In `persistent` mode that temporary worker saves the verified session to the OS credential store and exits. In `memory_only` and `one_shot` modes the isolated worker remains the credential owner for the bounded session lifetime.

## Security interpretation

The MCP tools are read-only. The underlying Avanza web session is not assumed to be read-only and must be treated as a high-value banking credential. Tool allowlists protect the MCP execution path; they do not constrain an attacker who independently steals and reuses the Avanza session.

The worker architecture therefore minimizes credential lifetime and process exposure rather than claiming the session itself has reduced Avanza privileges.
