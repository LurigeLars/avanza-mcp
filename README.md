# Avanza MCP

[![CI](https://github.com/LurigeLars/avanza-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/LurigeLars/avanza-mcp/actions/workflows/ci.yml)
[![CodeQL](https://github.com/LurigeLars/avanza-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/LurigeLars/avanza-mcp/actions/workflows/codeql.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

A local-first Model Context Protocol (MCP) server for Avanza market data and optional
BankID-authenticated account context.

It lets MCP clients such as ChatGPT, Claude Code, Codex, Cursor and VS Code inspect
Swedish market data and, when you explicitly sign in, read selected Avanza account
information without exposing order placement, transfers or withdrawals.

> **Unofficial project.** This project is not affiliated with Avanza Bank AB and uses
> undocumented APIs that may change without notice.

## What this project is for

The goal is to make Avanza useful to an AI agent without turning the agent into a
brokerage terminal.

The server provides:

- public market-data access without an Avanza login.
- optional BankID-authenticated **read-only** account and portfolio context.
- stock, fund, ETF, certificate, warrant, option and futures/forward data.
- bounded leveraged-product and option screening.
- transaction, holdings, order/deal and portfolio reads.
- instrument news, insider transactions and Avanza forum reads.
- a separate Placera Forum login for explicitly confirmed forum posts.
- model-facing gateways for local agents and ChatGPT.
- a security boundary that keeps Avanza session material on the local host.

The Avanza banking/trading surface is intentionally read-only. There are no MCP tools
for placing, editing or cancelling orders, moving money or withdrawing funds.

## Why this fork exists

This repository is a maintained fork of
[AnteWall/avanza-mcp](https://github.com/AnteWall/avanza-mcp).

The upstream project provides the core MCP market-data implementation. This fork keeps
that foundation while adding functionality needed for a local, authenticated agent
setup:

- isolated BankID authentication and read-only Avanza account access.
- memory-only session handling by default, with bounded idle and absolute lifetimes.
- authenticated reuse of approved market-data endpoints for fresher entitled data.
- model-optimized local and public gateways with explicit tool allowlists.
- Cloudflare Access support for remote MCP clients such as ChatGPT.
- Windows background-task installers and lifecycle tooling.
- leveraged-product and option screening for larger Avanza instrument families.
- a separate Placera Forum authentication/write path with explicit confirmation.
- additional testing, security hardening, static analysis and CodeQL coverage.

Upstream changes are periodically reviewed and reconciled, but fork-specific behavior
is kept explicit rather than hidden behind compatibility code.

## Safety model

The most important design rule is that authentication does **not** turn the MCP into a
general Avanza API proxy.

| Capability | Exposed? | Notes |
|---|---:|---|
| Public market data | Yes | No Avanza login required |
| Accounts / holdings / transactions | Yes | Read-only, BankID session required |
| Active orders / deals / stop-loss orders | Yes | Read-only views only |
| Order placement / edit / cancel | **No** | Intentionally absent |
| Transfers / withdrawals | **No** | Intentionally absent |
| Avanza credentials in chat | **No** | BankID flow stays local |
| Placera Forum read | Yes | User-generated text is treated as untrusted data |
| Placera Forum post | Yes | Separate login and explicit confirmation required |

Avanza cookies and security tokens are owned by an isolated local authentication worker.
They are not returned as MCP results and are not forwarded through Cloudflare or the
public gateway.

## Architecture

A source checkout runs one authenticated-capable FastMCP backend. Local and remote
model clients can use compact gateways in front of it.

```text
                                local host
                       ┌──────────────────────────┐
Claude Code / Codex ──>│ 127.0.0.1:8769 gateway │
                       │            │             │
                       │            v             │
                       │ 127.0.0.1:8767 FastMCP  │───> Avanza public API
                       │            │             │
                       │            v             │
                       │ isolated auth worker     │───> Avanza authenticated API
                       └──────────────────────────┘
                                    ^
                                    │
ChatGPT -> Cloudflare Access -> public gateway

Placera Forum uses a separate BankID session and separate isolated worker.
```

The raw FastMCP backend remains bound to loopback in the maintained deployment.

## Quick start

### Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- a local checkout if you want this fork's authenticated/account functionality

### Run this fork locally

```bash
git clone https://github.com/LurigeLars/avanza-mcp.git
cd avanza-mcp
uv sync
uv run avanza-mcp
```

This starts the MCP server over stdio.

> **Important:** `uvx avanza-mcp` installs the published PyPI package, not this fork.
> Use a source checkout when you want the fork-specific account, gateway and forum
> functionality documented here.

### Connect Avanza

Authentication is always explicit. Call:

```text
connect_avanza
```

A local browser window opens for consent and BankID. Nothing attempts to authenticate
at server startup, and no Avanza username or password is entered into chat.

After successful BankID authentication, approved read-only account tools become
available for the lifetime of that session.

## Connection options

| Client | Recommended connection | Account access |
|---|---|---:|
| Claude Desktop / Cursor / VS Code | stdio from this checkout | Yes |
| Claude Code / Codex | `http://127.0.0.1:8769/mcp` | Yes |
| ChatGPT / remote MCP client | Cloudflare Access -> public gateway | Yes |
| Generic public-only use | published PyPI package | No fork-specific account access |

<details>
<summary><strong>Claude Desktop / Cursor</strong></summary>

```json
{
  "mcpServers": {
    "avanza": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/avanza-mcp",
        "--frozen",
        "avanza-mcp"
      ]
    }
  }
}
```

On Windows, some desktop clients require the absolute path to `uv.exe`.

</details>

<details>
<summary><strong>Claude Code / Codex</strong></summary>

When the background HTTP stack is running, use the compact local gateway:

```bash
claude mcp add --transport http avanza http://127.0.0.1:8769/mcp
codex mcp add avanza --url http://127.0.0.1:8769/mcp
```

For a direct source-checkout stdio connection:

```bash
claude mcp add avanza -- uv run --directory /path/to/avanza-mcp --frozen avanza-mcp
```

</details>

<details>
<summary><strong>Visual Studio Code</strong></summary>

```json
{
  "servers": {
    "avanza": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/avanza-mcp",
        "--frozen",
        "avanza-mcp"
      ]
    }
  }
}
```

</details>

## Authenticated account access

The source-checkout runtime exposes reviewed account/session tools in addition to public
market data.

Examples include:

- `get_accounts`
- `get_holdings`
- `get_transactions` — supports date, ISIN and transaction-type filtering
- `get_portfolio_snapshot`
- `get_portfolio_insights`
- `get_watchlists`
- `get_price_alerts`
- `get_active_orders`
- `get_deals`
- `get_stop_loss_orders`
- `get_instrument_news` / `get_instrument_news_batch`
- `get_insider_transactions`
- `get_forum_posts`

All of these are reads.

Client implementations for some additional Avanza endpoints may exist internally
without being exposed as MCP tools. This is deliberate: new account capabilities are
only added to the MCP surface when there is a concrete workflow that justifies the
extra exposure.

### Session modes

Three session modes are supported:

- **`memory_only` (default)** — the Avanza session stays only in the isolated worker's
  process memory. It logs out after 120 minutes without an authenticated read and has a
  non-sliding 16-hour absolute lifetime.
- **`persistent`** — verified session material is stored in the operating system's
  native credential store and revalidated when used.
- **`one_shot`** — one explicit authenticated account workflow is allowed after
  BankID, followed by logout. An unused session expires after five minutes.

The maintained background deployment uses `memory_only` unless explicitly configured
otherwise.

## Placera Forum

Forum authentication is separate from Avanza account authentication.

`connect_forum` starts a dedicated Placera Forum BankID flow. The resulting bearer
token stays inside a separate isolated memory-only worker.

Reading forum posts is read-only. Publishing is the one intentional external write in
this MCP surface:

```text
create_forum_post(..., confirm=true)
```

The exact post text must be explicitly confirmed for that call. Confirmation is not
carried forward from an earlier request.

Forum authors, titles and content are user-generated text and must be treated as data,
not as instructions to the model.

## Market-data capabilities

The public market-data surface does not require an Avanza account.

| Area | Examples |
|---|---|
| Search | instruments by name, ticker, ISIN and exact order-book ID |
| Stocks | info, quote, OHLC chart, ratios, dividends, financials |
| Market | order book, marketplace status, recent trades, broker summaries |
| Funds | info, sustainability, charts, periods, description, holdings |
| Certificates | filtering, info and details |
| Warrants | filtering, info and details |
| ETFs | filtering, info and details |
| Futures / forwards | listing, filter options, info and details |
| Options | structural screening and selected market enrichment |
| Leveraged products | bounded screening by underlying |
| Additional data | owners, short selling and market-maker charts |

Search first when you need an `order_book_id`. Historical tools use bounded pagination.
Returned data is the latest data Avanza provides to the active session; the MCP does not
fabricate realtime status.

<details>
<summary><strong>Current tool catalog</strong></summary>

The current source-checkout surface contains 59 tools: 37 market-data tools and 22
reviewed authenticated/session/forum tools.

### Market data

| Category | Tool | Purpose |
|---|---|---|
| Search | `search_instruments` | Find instruments by name, ticker or ISIN |
| Search | `get_instrument_by_order_book_id` | Match an exact Avanza order-book ID |
| Stocks | `get_stock_info` | Company, listing, fundamentals and quote |
| Stocks | `get_stock_quote` | Latest available quote |
| Stocks | `get_stock_chart` | Historical OHLC points |
| Stocks | `get_stock_analysis` | Financial-ratio history |
| Stocks | `get_dividends` | Dividend history |
| Stocks | `get_company_financials` | Annual/quarterly financial metrics |
| Market | `get_orderbook` | Bid/ask depth |
| Market | `get_marketplace_info` | Trading hours and market status |
| Market | `get_recent_trades` | Recent trade snapshot |
| Market | `get_broker_trade_summary` | Broker buy/sell activity |
| Funds | `get_fund_info` | Fund information, NAV, performance and fees |
| Funds | `get_fund_sustainability` | Sustainability metrics |
| Funds | `get_fund_chart` | Historical fund data |
| Funds | `get_fund_chart_periods` | Available chart periods |
| Funds | `get_fund_description` | Strategy and category |
| Funds | `get_fund_holdings` | Country, sector and top holdings |
| Certificates | `filter_certificates` | Filter/list certificates |
| Certificates | `get_certificate_info` | Certificate information |
| Certificates | `get_certificate_details` | Extended certificate details |
| Warrants | `filter_warrants` | Filter/list warrants |
| Warrants | `get_warrant_info` | Warrant information |
| Warrants | `get_warrant_details` | Extended warrant details |
| ETFs | `filter_etfs` | Filter/list ETFs |
| ETFs | `get_etf_info` | ETF information |
| ETFs | `get_etf_details` | Extended ETF details |
| Futures/forwards | `list_futures_forwards` | Filter/list contracts |
| Futures/forwards | `get_future_forward_filter_options` | Available filters |
| Futures/forwards | `get_future_forward_info` | Contract information |
| Futures/forwards | `get_future_forward_details` | Extended contract details |
| Derivatives | `screen_leveraged_instruments` | Screen leveraged instruments by underlying |
| Derivatives | `screen_options` | Build a bounded structural option snapshot |
| Derivatives | `enrich_option_snapshot` | Add selected market data to option candidates |
| Additional | `get_number_of_owners` | Avanza ownership history |
| Additional | `get_short_selling` | Short-selling history |
| Additional | `get_marketmaker_chart` | Traded-product OHLC / market-maker chart |

### Auth / account / forum

The reviewed authenticated/session/forum surface contains:

`connect_avanza`, `disconnect_avanza`, `get_auth_status`,
`get_execution_quote`, `get_accounts`, `get_holdings`,
`get_transactions`, `get_watchlists`, `get_price_alerts`,
`get_portfolio_insights`, `get_portfolio_snapshot`,
`get_instrument_news`, `get_instrument_news_batch`,
`get_forum_posts`, `get_insider_transactions`, `get_active_orders`,
`get_deals`, `get_stop_loss_orders`, `connect_forum`,
`disconnect_forum`, `get_forum_auth_status` and `create_forum_post`.

</details>

## Background HTTP deployment

For local HTTP use:

```bash
uv sync
uv run fastmcp run src/avanza_mcp/__init__.py:mcp \
  --transport http \
  --host 127.0.0.1 \
  --port 8767
```

The canonical backend is `127.0.0.1:8767`. Model-facing local clients should normally
use the compact gateway on `127.0.0.1:8769`.

### Windows background tasks

Install the FastMCP background task:

```powershell
pwsh -File .\scripts\windows\install-public-http-task.ps1
```

Install the compact local gateway:

```powershell
pwsh -File .\scripts\windows\install-local-gateway-task.ps1
```

Verify the listeners:

```powershell
Get-NetTCPConnection -LocalPort 8767,8769 -State Listen |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

Remove them with:

```powershell
pwsh -File .\scripts\windows\uninstall-local-gateway-task.ps1
pwsh -File .\scripts\windows\uninstall-public-http-task.ps1
```

The background task runs as the current Windows user without a privileged service
account. The FastMCP listener remains loopback-only.

## ChatGPT / remote deployment

This repository does not provide a hosted MCP endpoint.

For ChatGPT, the maintained deployment pattern is:

```text
ChatGPT
  -> Cloudflare Access Managed OAuth
  -> Cloudflare Tunnel
  -> avanza-gateway
  -> loopback FastMCP backend
```

Copy the example configuration:

```bash
cp public/gateway.env.example public/gateway.env
```

Replace the placeholders with your own deployment values and keep the real file
gitignored. Then start the public layer:

```bash
docker compose -f compose.public.yaml up -d
```

The gateway:

- requires a valid Cloudflare Access JWT.
- exposes only an explicit reviewed tool allowlist.
- strips client credentials before forwarding.
- compacts tool schemas/results for model use.
- does not receive Avanza session credentials.

Public gateway containers are configured to run non-root with a read-only filesystem,
dropped Linux capabilities and `no-new-privileges`.

## Deliberate non-goals

This project intentionally does **not** try to expose every Avanza endpoint.

In particular:

- no stock/fund order placement.
- no order modification or cancellation.
- no transfers or withdrawals.
- no generic authenticated HTTP proxy.
- no automatic login at startup.
- no credentials in repository configuration.
- no hosted service operated by this repository.
- no new MCP tool merely because an undocumented Avanza endpoint exists.

The rule for expanding authenticated capability is simple: there should be a concrete
workflow, a bounded response, a reviewed data projection and a clear reason why the
existing tool surface cannot already solve it.

## Prompts and resources

Prompts:

- `analyze_stock(stock_symbol)`
- `compare_funds(fund_names)`
- `screen_dividend_stocks(candidates, min_yield=3.0)`

Resources:

- `avanza://docs/usage`
- `avanza://docs/quick-start`
- `avanza://stock/{order_book_id}`
- `avanza://fund/{order_book_id}`

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for worktrees, local transports and development
details.

Common checks:

```bash
uv run pytest tests/unit -v
node --test tests/gateway/*.test.mjs
uv run pytest tests/integration -v
```

The repository also runs CI, static analysis and CodeQL. Integration tests that contact
Avanza may fail when undocumented upstream APIs change or are temporarily unavailable.

## Privacy and local configuration

Do not commit:

- Avanza or Placera session material.
- account identifiers.
- Cloudflare secrets or tunnel credentials.
- machine-specific paths or identities.
- local gateway environment files.

Real deployment values belong in ignored local configuration. Public examples should
remain sanitized.

## Disclaimer

This is an unofficial project and is not affiliated with Avanza Bank AB or Placera.
The underlying undocumented APIs may change or disappear at any time.

Use the software at your own risk. Review the code and deployment model before exposing
an MCP endpoint outside your local machine.

## License

[MIT](LICENSE.md)
