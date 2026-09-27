# Avanza MCP Server

## About this fork

This is a maintained fork of [AnteWall/avanza-mcp](https://github.com/AnteWall/avanza-mcp). It keeps the upstream read-only market-data surface while adding a local-first deployment and agent layer.

Fork-specific changes include:

- Optional BankID-authenticated **read-only** account access with native OS credential storage; no order placement, order editing, transfers, or withdrawals.
- Bounded leveraged-product and options screening, with pagination and concurrency improvements for large Avanza instrument families.
- Compact model-facing MCP gateways for local agents and ChatGPT, with explicit tool allowlists, schema/result compaction, and duplicate structured-result suppression.
- Cloudflare Access support with a shared-tunnel deployment model and sanitized public configuration templates.
- Windows background-task installers and local service lifecycle helpers.
- Additional security hardening, tests, Dependabot coverage, and fork-specific Advanced CodeQL scanning.

Upstream changes are periodically reconciled while the fork-specific behavior above remains explicit.

> **Want to use Avanza with Agents?** Consider the [Avanza CLI](https://antewall.github.io/avanza-ts/docs/cli/) with [agent skills](https://antewall.github.io/avanza-ts/docs/cli/skills/) instead. It may be a better fit for agent workflows.

![PyPI - Version](https://img.shields.io/pypi/v/avanza-mcp)
[![CI](https://github.com/AnteWall/avanza-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AnteWall/avanza-mcp/actions/workflows/ci.yml)

Avanza's public market data is available read-only without an Avanza account. This fork also supports optional locally authenticated, read-only account access.

## Disclaimer

This is an unofficial API client/MCP Server. Not affiliated with Avanza Bank AB. The underlying API can be taken down or changed without warning at any point in time.

The author of this software is not responsible for any indirect damages (foreseeable or unforeseeable), such as, if necessary, loss or alteration of or fraudulent access to data, accidental transmission of viruses or of any other harmful element, loss of profits or opportunities, the cost of replacement goods and services or the attitude and behavior of a third party.

## Features

- Stocks, funds, ETFs, certificates, warrants and futures/forwards.
- Quotes, charts, financial ratios, dividends, order books and ownership data.
- Fund performance, fees, holdings, sustainability and research prompts.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

### Which connection, and what it gives you

| Client | Connection | Account access |
|---|---|---|
| Claude Desktop, Cursor, VS Code | local stdio from a checkout | **yes**, with `AVANZA_MCP_AUTH=1` |
| Claude Code, Codex | local loopback gateway `127.0.0.1:8769` | yes, when the background stack runs with auth |
| ChatGPT and other cloud chats | Cloudflare Access -> gateway -> `127.0.0.1:8767` | **yes**, when the gateway uses the `@authenticated` profile |
| Any client, no checkout | `uvx avanza-mcp` from PyPI | **no** |

**`AVANZA_MCP_AUTH=1` is the switch.** Unset, `main()` serves the 37-tool public
market-data surface. Set to `1`, it starts the authenticated server instead and adds 14 reviewed
session/account tools: `connect_avanza`, `disconnect_avanza`, `get_auth_status`, `get_accounts`,
`get_holdings`, `get_transactions`, `get_watchlists`, `get_price_alerts`,
`get_portfolio_insights`, `get_instrument_news`, `get_insider_transactions`,
`get_active_orders`, `get_deals`, and `get_stop_loss_orders`. The combined surface is 51 tools.
The screening tools `screen_options`, `screen_leveraged_instruments`, and
`enrich_option_snapshot` are already part of the public 37-tool surface. Authentication happens
when `connect_avanza` is called, through a local browser and BankID; nothing is requested at
startup and banking credentials never pass through the chat. The MCP surface exposes no order
placement, editing, transfers, or withdrawals.

`uvx avanza-mcp` installs the published PyPI package, which is **not this fork** and has no account
access. Use it only when you have no checkout, and do not expect holdings from it.

<details>
<summary>Claude Desktop and Cursor</summary>

Both launch the server over stdio from a checkout. Replace the path with your own:

```json
{
  "mcpServers": {
    "avanza": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/avanza-mcp", "--frozen", "avanza-mcp"],
      "env": { "AVANZA_MCP_AUTH": "1" }
    }
  }
}
```

- **Claude Desktop:** open Settings > Developer > Edit Config, merge the configuration, then fully
  restart Claude Desktop. On Windows give `command` the absolute path to `uv.exe`, since Desktop
  does not resolve it from `PATH`.
- **Cursor:** add it to `.cursor/mcp.json` in your project, or `~/.cursor/mcp.json` globally. Enable
  the server in Cursor's MCP settings.

Drop the `env` block for public market data only. To check which surface you got, look at the
server name in the handshake: `Avanza MCP Authenticated Server` against `Avanza MCP`.

</details>

<details>
<summary>Claude Code and Codex</summary>

For a source checkout running the background HTTP stack, prefer the model-optimized
loopback gateway:

```bash
claude mcp add --transport http avanza http://127.0.0.1:8769/mcp
codex mcp add avanza --url http://127.0.0.1:8769/mcp
```

This keeps the canonical FastMCP server on port 8767 while presenting the compact
37-tool catalog on port 8769. Use `/mcp` in Claude Code or `codex mcp list` to
verify the connection.

The portable stdio form remains available when no background HTTP stack is installed. It is the
PyPI package, so it serves public market data only:

```bash
claude mcp add avanza -- uvx avanza-mcp
```

For the account surface without the background stack, point the client at a checkout instead:

```bash
claude mcp add avanza --env AVANZA_MCP_AUTH=1 -- uv run --directory /path/to/avanza-mcp --frozen avanza-mcp
```

The stdio form talks directly to FastMCP and therefore exposes the full raw schemas.

</details>

<details>
<summary>Visual Studio Code</summary>

Add to `.vscode/mcp.json`, or open **MCP: Open User Configuration** for global setup:

```json
{
  "servers": {
    "avanza": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/avanza-mcp", "--frozen", "avanza-mcp"],
      "env": { "AVANZA_MCP_AUTH": "1" }
    }
  }
}
```

Run **MCP: List Servers**, start `avanza`, and enable its tools in agent chat.

</details>

<details>
<summary>OpenCode</summary>

Add to your project's `opencode.json` or global `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "avanza": {
      "type": "local",
      "command": ["uvx", "avanza-mcp"],
      "enabled": true
    }
  }
}
```

</details>

<details>
<summary>HTTP, local agents and ChatGPT</summary>

From a source checkout, run one loopback-only Streamable HTTP server:

```bash
uv sync
uv run fastmcp run src/avanza_mcp/__init__.py:mcp --transport http --host 127.0.0.1 --port 8767
```

The FastMCP server on `127.0.0.1:8767` is the canonical backend. Model-facing local
clients should use the compact loopback gateway on `127.0.0.1:8769` instead. It
applies the same allowlist and schema compaction used by the ChatGPT path and removes
duplicate `structuredContent` only when FastMCP also returned the same tool result as
text `content`.

```text
Claude Code / Codex -> 127.0.0.1:8769/mcp -> compact model gateway
                                             -> 127.0.0.1:8767/mcp -> FastMCP
ChatGPT -> Cloudflare Access -> public gateway -> 127.0.0.1:8767/mcp
```

Keep direct `8767` access for development, typed-contract tests, or clients that
specifically require the full output schemas/structured results.

On Windows, the HTTP server can run without a visible terminal window using the
included Scheduled Task installer:

```powershell
pwsh -File .\scripts\windows\install-public-http-task.ps1
```

The default session mode is `persistent`. To choose a stricter mode when installing
or replacing the task:

```powershell
pwsh -File .\scripts\windows\install-public-http-task.ps1 -SessionMode memory_only
pwsh -File .\scripts\windows\install-public-http-task.ps1 -SessionMode one_shot
```

The task uses the repository virtualenv's `pythonw.exe`, so no console window is created. It runs as the current Windows user with limited privileges, starts at logon, and also has a five-minute recovery trigger with `IgnoreNew` so an already-running server is never duplicated. No Windows password is stored. The FastMCP endpoint remains bound to `127.0.0.1:8767`. Logs are written to `%LOCALAPPDATA%\avanza-mcp\public-http.log` and rotated once at 5 MiB.

If port 8767 is already occupied by a manually started FastMCP process, the installer
registers the task but deliberately does not kill or replace that process. Stop the
manual server with Ctrl+C, then start the hidden task:

```powershell
Start-ScheduledTask -TaskName "AvanzaMcpHttpServer"
```

Check it with:

```powershell
Get-ScheduledTask -TaskName "AvanzaMcpHttpServer"
Get-NetTCPConnection -LocalPort 8767 -State Listen
```

Install the model-optimized local gateway as a second hidden Windows task:

```powershell
pwsh -File .\scripts\windows\install-local-gateway-task.ps1
```

Verify both loopback listeners:

```powershell
Get-NetTCPConnection -LocalPort 8769,8767 -State Listen |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

Use `http://127.0.0.1:8769/mcp` for Claude Code and Codex. The local gateway binds
only to loopback, accepts only loopback clients, forwards only to a loopback upstream,
stores no credentials, and uses the same explicit read-only allowlist as the public
gateway.

For Codex CLI, an HTTP MCP server can be configured with:

```bash
codex mcp add avanza --url http://127.0.0.1:8769/mcp
```

For Claude Code:

```bash
claude mcp add --transport http avanza http://127.0.0.1:8769/mcp
```

If an `avanza` MCP entry already exists, update/remove that entry first rather than
creating two servers with the same capability.

Remove the background tasks with:

```powershell
pwsh -File .\scripts\windows\uninstall-local-gateway-task.ps1
pwsh -File .\scripts\windows\uninstall-public-http-task.ps1
```

For ChatGPT web, keep the FastMCP endpoint on loopback and use the included public deployment layer:

```text
ChatGPT -> Cloudflare Access Managed OAuth -> shared Cloudflare Tunnel
        -> avanza-gateway:8080 -> 127.0.0.1:8767/mcp
```

Copy `public/gateway.env.example` to `public/gateway.env` and replace every placeholder with your own deployment values. Keep the real file gitignored.
Configure the Cloudflare Access application and shared tunnel route for your own hostname, then start:

```bash
docker compose -f compose.public.yaml up -d
```

The public gateway requires a valid Cloudflare Access JWT. Both model-facing gateway
modes restrict calls to the explicit current 37-tool read-only allowlist, strip client
credentials before forwarding, compact tool schemas to reduce model-context overhead,
and remove duplicate structured tool-result payloads when an equivalent text result is
already present. New MCP tools are not exposed through either model-facing gateway
until the allowlist is reviewed.

This project does not provide a hosted endpoint. The public connector remains
credential-free and cannot access Avanza accounts or place orders.

Optional authenticated read-only access uses a loopback BankID flow and an isolated
auth-worker architecture. The long-lived FastMCP/control-plane process does not receive
Avanza cookies or the security token. When a valid Avanza session exists, the existing
public market-data tools reuse it through an explicit read-only endpoint allowlist so
Avanza can return the fresher/realtime data entitled to the logged-in session. This
includes stocks, certificates, warrants, leveraged screening, ETFs, options/futures,
funds and the other existing public market-data tools; realtime availability remains
an upstream Avanza property and is not fabricated by the MCP.

Three session modes are available:

- `persistent` (default): the verified Avanza session is stored in the native OS
  credential store. Each authenticated operation starts a short-lived worker, which
  loads the session once and runs session validation concurrently with the approved
  read. The result is released only after validation succeeds; refreshed material is
  persisted only when it changed. The worker then closes its HTTP clients and exits.
- `memory_only`: no reusable Avanza session is written to the OS credential store.
  A dedicated isolated worker keeps the session only in its process memory and performs
  remote logout plus exits after 15 minutes without an authenticated read-only operation.
- `one_shot`: no persistent session is written. After BankID, the isolated worker
  permits one explicit authenticated account workflow, performs remote logout, and exits.
  If unused, it logs out after five minutes. Approved public market-data calls may reuse
  the in-memory session during that bounded window.

The Cloudflare gateway may expose the reviewed authenticated read-only MCP tools when
configured with the `@authenticated` profile, but Avanza session credentials remain on
the Windows host and never traverse Docker, Cloudflare, or the MCP result channel.
There are no order-placement, order-edit, cancellation, transfer, or withdrawal tools.

Intentionally not exposed by the authenticated MCP surface: `get_credit_info`,
`get_current_offers`, and `get_forum_posts`. Their client implementations are retained
for future reviewed activation if a concrete workflow requires them.

</details>

<details>
<summary>Python</summary>

Install with `uv add avanza-mcp`, then use FastMCP's in-process client:

```python
import asyncio
from fastmcp import Client
from avanza_mcp import mcp

async def main():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_instruments", {"query": "Volvo", "instrument_type": "stock"}
        )
        print(result.structured_content)

asyncio.run(main())
```

</details>

If a desktop client cannot find `uvx`, use its absolute executable path. See [DEVELOPMENT.md](DEVELOPMENT.md) for running from source and tests.

## Tools

The public market-data surface exposes 37 read-only tools. Search first to obtain an `order_book_id`; history tools expose pagination. Data is latest available, not guaranteed live.

| Category | Tool | Description |
|----------|------|-------------|
| Search | `search_instruments` | Find instruments by name, ticker or ISIN |
| Search | `get_instrument_by_order_book_id` | Match an exact ID within search candidates |
| Stocks | `get_stock_info` | Company, listing, fundamentals and quote |
| Stocks | `get_stock_quote` | Latest price and trading volume |
| Stocks | `get_stock_chart` | Historical OHLC price points |
| Stocks | `get_stock_analysis` | A named financial-ratio history |
| Stocks | `get_dividends` | A named dividend metric by financial year |
| Stocks | `get_company_financials` | A named annual or quarterly financial metric |
| Market | `get_orderbook` | Bid/ask depth |
| Market | `get_marketplace_info` | Trading hours and market status |
| Market | `get_recent_trades` | Recent trade snapshot |
| Market | `get_broker_trade_summary` | Broker buy/sell activity |
| Derivatives | `screen_leveraged_instruments` | Bounded certificate/warrant screen for one underlying |
| Derivatives | `screen_options` | Bounded structural options screen for one underlying |
| Derivatives | `enrich_option_snapshot` | Add selected market snapshot data to option candidates |
| Funds | `get_fund_info` | NAV, performance, fees and fund information |
| Funds | `get_fund_sustainability` | ESG and sustainability metrics |
| Funds | `get_fund_chart` | Historical fund chart points |
| Funds | `get_fund_chart_periods` | Available performance periods |
| Funds | `get_fund_description` | Investment strategy and category |
| Funds | `get_fund_holdings` | Country, sector and top-holding allocations |
| Certificates | `filter_certificates` | Filter and list certificates |
| Certificates | `get_certificate_info` | Certificate information |
| Certificates | `get_certificate_details` | Extended certificate details |
| Warrants | `filter_warrants` | Filter and list warrants |
| Warrants | `get_warrant_info` | Warrant information |
| Warrants | `get_warrant_details` | Extended warrant details |
| ETFs | `filter_etfs` | Filter and list ETFs |
| ETFs | `get_etf_info` | ETF information |
| ETFs | `get_etf_details` | Extended ETF details |
| Futures/Forwards | `list_futures_forwards` | Filter and list contracts |
| Futures/Forwards | `get_future_forward_filter_options` | Available contract filters |
| Futures/Forwards | `get_future_forward_info` | Contract information |
| Futures/Forwards | `get_future_forward_details` | Extended contract details |
| Additional | `get_number_of_owners` | Avanza ownership history |
| Additional | `get_short_selling` | Short-selling history |
| Additional | `get_marketmaker_chart` | Traded-product OHLC and market-maker data |

Authenticated mode keeps the same 37 public market-data tools and adds reviewed read-only
account/activity tools. With the gateway's `@authenticated` profile, that combined
surface is available behind Cloudflare Access.

## Prompts

- `analyze_stock(stock_symbol)` - Research a stock's fundamentals and price history.
- `compare_funds(fund_names)` - Compare two or more supplied funds.
- `screen_dividend_stocks(candidates, min_yield=3.0)` - Screen supplied stocks by dividend yield.

## Resources

- `avanza://docs/usage` - Tool usage guide.
- `avanza://docs/quick-start` - Common workflows.
- `avanza://stock/{order_book_id}` - Stock summary as Markdown.
- `avanza://fund/{order_book_id}` - Fund summary as Markdown.

## License

[MIT](LICENSE.md)
