# Remote authenticated read-only Avanza MCP

## Scope

This document covers remote ChatGPT access to the existing authenticated read-only Avanza MCP surface. It does not add a second Avanza connector and does not add trading/write capabilities.

The canonical architecture remains:

```text
ChatGPT
  -> Cloudflare Access / Managed OAuth
  -> existing avanza-mcp-gateway
  -> 127.0.0.1:8767 on the Windows host
  -> create_auth_server()
       -> authenticated read-only tools
       -> mounted public market-data MCP
  -> Windows Credential Manager
  -> Avanza
```

The local model-optimized gateway remains on `127.0.0.1:8769` and forwards to the same canonical server on `8767`.

## One connector, one server

The authenticated FastMCP server already mounts the public MCP. Remote deployment therefore starts `create_auth_server()` directly as the canonical loopback HTTP server instead of running the public-only `mcp` object.

The gateway uses the reserved `ALLOWED_TOOLS=@authenticated` profile. That profile is the union of:

- the existing 37 public market-data tools; and
- 14 reviewed session/account read-only tools.

Total model-visible surface: 51 tools.

The following internally implemented upstream capabilities remain intentionally absent from the model surface:

- `get_credit_info`
- `get_current_offers`
- `get_forum_posts`

No order placement, order modification/cancellation, money movement, arbitrary authenticated request tool, or generic network tool is added.

## Credential boundary

Avanza session cookies and `X-SecurityToken` remain on the Windows host.

They are:

- created/validated by the reviewed BankID client;
- persisted only through the native OS credential store;
- copied only into the host-side authenticated Avanza HTTP client;
- never returned from an MCP tool;
- never forwarded through Cloudflare;
- never injected by the gateway.

The Cloudflare-facing gateway strips client authorization/cookie/Cloudflare forwarding headers before forwarding MCP traffic to `8767`.

Remote MCP calls may return the explicitly modeled private account data requested by the user. That private result data necessarily traverses the existing encrypted MCP/Cloudflare/ChatGPT path; the Avanza session credential itself does not.

## BankID boundary

`connect_avanza` may be invoked through the remote connector, but the approval/QR page remains a temporary loopback listener bound to `127.0.0.1` on the Windows host.

The QR payload, BankID transaction material and local CSRF/path tokens are not returned through MCP. The remote tool returns only safe connection state.

## Realtime behavior

Only exact approved stock GET paths for quote, order depth and recent trades may reuse the authenticated session. Authentication expiry fails closed for that request; the same request is not silently retried anonymously.

## Gateway boundary

Cloudflare Access remains the external identity boundary. The gateway continues to verify signature, issuer, audience and explicitly allowed email before forwarding.

The gateway still:

- accepts only `/mcp`;
- rate-limits by authenticated Access identity;
- enforces request-size limits;
- strips inbound credentials/forwarding headers;
- filters `tools/list`;
- blocks non-allowlisted `tools/call` before upstream execution;
- compacts tool schemas and duplicate result representations.

## Activation gate

This branch must not be deployed or used with a remote authenticated account session until an independent security/code review approves the remote boundary.

Required review focus:

1. same-connector tool-surface correctness (51 tools, hidden three absent);
2. Cloudflare Access identity boundary;
3. proof that Avanza cookies/security token never cross the gateway;
4. loopback-only BankID approval page under remote invocation;
5. account-result privacy and logging behavior;
6. fail-closed auth expiry/realtime behavior;
7. no trading/write capability;
8. scheduled-task lifecycle and local browser behavior when invoked from ChatGPT.
