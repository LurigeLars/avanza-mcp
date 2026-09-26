# Avanza MCP Server

> **Want to use Avanza with Agents?** Consider the [Avanza CLI](https://antewall.github.io/avanza-ts/docs/cli/) with [agent skills](https://antewall.github.io/avanza-ts/docs/cli/skills/) instead. It may be a better fit for agent workflows.

![PyPI - Version](https://img.shields.io/pypi/v/avanza-mcp)
[![CI](https://github.com/AnteWall/avanza-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AnteWall/avanza-mcp/actions/workflows/ci.yml)

Read-only access to Avanza's public market data from your MCP client. No Avanza account required.

## Disclaimer

This is an unofficial API client/MCP Server. Not affiliated with Avanza Bank AB. The underlying API can be taken down or changed without warning at any point in time.

The author of this software is not responsible for any indirect damages (foreseeable or unforeseeable), such as, if necessary, loss or alteration of or fraudulent access to data, accidental transmission of viruses or of any other harmful element, loss of profits or opportunities, the cost of replacement goods and services or the attitude and behavior of a third party.

## Features

- Stocks, funds, ETFs, certificates, warrants and futures/forwards.
- Quotes, charts, financial ratios, dividends, order books and ownership data.
- Fund performance, fees, holdings, sustainability and research prompts.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+. Local clients launch the server over stdio.

<details>
<summary>Claude Desktop and Cursor</summary>

Both use this configuration:

```json
{
  "mcpServers": {
    "avanza": {
      "command": "uvx",
      "args": ["avanza-mcp"]
    }
  }
}
```

- **Claude Desktop:** open Settings > Developer > Edit Config, merge the configuration, then fully restart Claude Desktop.
- **Cursor:** add it to `.cursor/mcp.json` in your project, or `~/.cursor/mcp.json` globally. Enable the server in Cursor's MCP settings.

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
35-tool catalog on port 8769. Use `/mcp` in Claude Code or `codex mcp list` to
verify the connection.

The portable stdio form remains available when no background HTTP stack is installed:

```bash
claude mcp add avanza -- uvx avanza-mcp
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
      "command": "uvx",
      "args": ["avanza-mcp"]
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

Provide the local gateway configuration out of band and keep it gitignored.
Configure the Cloudflare Access application and shared tunnel route outside this repository, then start:

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

Optional authenticated read-only access is available locally when explicitly enabled in local configuration.
It uses a loopback BankID flow, stores the verified session in the native OS credential
store, and reuses that session for market-data requests plus selected portfolio/activity
reads. It has no order-placement, order-edit, cancellation, transfer, or withdrawal tools.
The public Cloudflare gateway does not expose authenticated account tools.

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

All 35 tools are read-only. Search first to obtain an `order_book_id`; history tools expose pagination. Data is latest available, not guaranteed live.

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
