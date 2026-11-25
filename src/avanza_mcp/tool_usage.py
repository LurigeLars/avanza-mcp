"""Local, metadata-only per-tool counts and latency histograms.

No request arguments, response contents, identity, or free-form exceptions are stored.
The SQLite database is separate from financial data; one row per day/tool/bucket/code.
"""
from __future__ import annotations

import atexit
import os
import re
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic, perf_counter
from typing import Annotated

from fastmcp.server.middleware import Middleware
from pydantic import Field

BUCKETS = (10, 25, 50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000,
           5000, 10000, 20000, 30000, 60000, 120000, 300000, 600000, 86400000)
CODES = frozenset({"OK", "AUTH", "TIMEOUT", "RATE_LIMIT", "VALIDATION",
                   "UPSTREAM", "CANCELLED", "UNCLASSIFIED_ERROR", "HANDLER_EXCEPTION"})
RETENTION = 30
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def exception_code(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__
    if status in (401, 403) or name in ("AuthenticationError", "AuthorizationError"):
        return "AUTH"
    if status in (408, 504) or isinstance(exc, TimeoutError) or name.endswith("Timeout"):
        return "TIMEOUT"
    if status == 429:
        return "RATE_LIMIT"
    if status in (400, 422) or name in ("ValidationError", "ValueError"):
        return "VALIDATION"
    if type(status) is int and status >= 500:
        return "UPSTREAM"
    if name in ("CancelledError", "Cancelled"):
        return "CANCELLED"
    return "HANDLER_EXCEPTION"


def result_code(result: object) -> str:
    data = result if isinstance(result, Mapping) else getattr(result, "structured_content", None)
    error = (result.get("isError", False) if isinstance(result, Mapping)
             else getattr(result, "is_error", False))
    if isinstance(data, Mapping):
        error = error or data.get("success") is False
        error = error or data.get("state") in ("error", "timed_out", "denied")
        error = error or (data.get("success") is not True and bool(data.get("error")))
        if error:
            if data.get("state") == "timed_out":
                return "TIMEOUT"
            if data.get("state") == "denied":
                return "AUTH"
            code = data.get("error_code")
            if isinstance(code, str) and code in CODES and code != "OK":
                return code
    return "UNCLASSIFIED_ERROR" if error else "OK"


class UsageStore:
    def __init__(self, path: Path, tools: frozenset[str], *, enabled: bool = True):
        self.path = path
        self.tools = tools - {"tool_usage"}
        self.enabled = enabled
        self._lock = Lock()
        self._pending: dict[tuple, list[int]] = {}
        self._count = 0
        self._last_flush = monotonic()
        self.recording_errors = 0
        self.dropped_calls = 0

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(self.path, timeout=0.05)
        conn.execute("""CREATE TABLE IF NOT EXISTS usage(
            day TEXT NOT NULL, tool TEXT NOT NULL, bucket INTEGER NOT NULL,
            code TEXT NOT NULL, calls INTEGER NOT NULL, sum_ms INTEGER NOT NULL,
            max_ms INTEGER NOT NULL, PRIMARY KEY(day,tool,bucket,code))""")
        return conn

    def _flush(self):
        if not self.enabled or not self._pending:
            return
        cutoff = (datetime.now(UTC).date() - timedelta(days=RETENTION-1)).isoformat()
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM usage WHERE day < ?", (cutoff,))
                conn.executemany(
                    """INSERT INTO usage VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(day,tool,bucket,code) DO UPDATE SET
                    calls=calls+excluded.calls, sum_ms=sum_ms+excluded.sum_ms,
                    max_ms=max(max_ms,excluded.max_ms)""",
                    [(*key, *value) for key, value in self._pending.items()],
                )
            self._pending.clear()
            self._count = 0
            self._last_flush = monotonic()
        except (OSError, sqlite3.Error):
            self.recording_errors += 1
            # Keep a bounded retry buffer rather than silently lose a whole batch.
            if self._count >= 512:
                self.dropped_calls += self._count
                self._pending.clear()
                self._count = 0

    def flush(self):
        with self._lock:
            self._flush()

    def record(self, name: str, elapsed_ms: float, code: str):
        if not self.enabled or name not in self.tools or os.getenv("PYTEST_CURRENT_TEST"):
            return
        if code not in CODES:
            code = "UNCLASSIFIED_ERROR"
        ms = max(0, min(86400000, round(elapsed_ms)))
        key = (utc_day(), name, bisect_left(BUCKETS, ms), code)
        with self._lock:
            entry = self._pending.setdefault(key, [0, 0, 0])
            entry[0] += 1
            entry[1] += ms
            entry[2] = max(entry[2], ms)
            self._count += 1
            if self._count >= 64 or monotonic() - self._last_flush >= 5:
                self._flush()

    def report(self, days: int = 7, limit: int = 50, tool: str | None = None) -> dict:
        if not 1 <= days <= RETENTION or not 1 <= limit <= 100:
            raise ValueError("Invalid report bounds")
        if tool is not None and not NAME.fullmatch(tool):
            raise ValueError("Invalid tool name")
        first = (datetime.now(UTC).date() - timedelta(days=days-1)).isoformat()
        today = utc_day()
        with self._lock:
            self._flush()
            rows = []
            if self.path.is_file():
                with closing(self._connect()) as conn, conn:
                    cutoff = (datetime.now(UTC).date()
                              - timedelta(days=RETENTION-1)).isoformat()
                    conn.execute("DELETE FROM usage WHERE day < ?", (cutoff,))
                    rows = conn.execute(
                        "SELECT day,tool,bucket,code,calls,sum_ms,max_ms FROM usage "
                        "WHERE day BETWEEN ? AND ? AND (? IS NULL OR tool=?)",
                        (first, today, tool, tool),
                    ).fetchall()
            pending = self._count
            errors, dropped = self.recording_errors, self.dropped_calls
        totals = {}
        sampled_days = set()
        for day, name, bucket, code, n, elapsed, maximum in rows:
            if not NAME.fullmatch(name) or not 0 <= bucket < len(BUCKETS) or code not in CODES:
                continue
            sampled_days.add(day)
            a = totals.setdefault(name, {"calls": 0, "errors": 0, "sum_ms": 0,
                                         "max_ms": 0, "bins": defaultdict(int),
                                         "error_codes": defaultdict(int)})
            a["calls"] += n
            a["sum_ms"] += elapsed
            a["max_ms"] = max(a["max_ms"], maximum)
            a["bins"][bucket] += n
            if code != "OK":
                a["errors"] += n
                a["error_codes"][code] += n

        def upper(a, fraction):
            rank = a["calls"] * fraction
            seen = 0
            for bucket, n in sorted(a["bins"].items()):
                seen += n
                if seen >= rank:
                    return min(BUCKETS[bucket], a["max_ms"])
            return None

        ranked = []
        for name, a in totals.items():
            ranked.append({"tool": name, "calls": a["calls"], "errors": a["errors"],
                           "error_rate": round(a["errors"]/a["calls"], 4),
                           "mean_ms": round(a["sum_ms"]/a["calls"]),
                           "p50_upper_ms": upper(a, .50), "p95_upper_ms": upper(a, .95),
                           "max_ms": a["max_ms"], "error_codes": dict(a["error_codes"])})
        ranked.sort(key=lambda r: (-r["calls"], r["tool"]))
        inventory = {n for n in self.tools if tool is None or n == tool}
        return {"success": True, "enabled": self.enabled,
                "from_utc_day": first, "to_utc_day": today, "retention_days": RETENTION,
                "recorded_calls": sum(r["calls"] for r in ranked),
                "recorded_errors": sum(r["errors"] for r in ranked),
                "registered_tool_count": len(inventory),
                "unobserved_tools": sorted(inventory - totals.keys()),
                "tool_count": len(ranked), "returned_count": min(limit, len(ranked)),
                "truncated": len(ranked) > limit, "days_with_samples": sorted(sampled_days),
                "pending_calls": pending, "recording_errors_since_start": errors,
                "dropped_calls_since_start": dropped,
                "measurement_scope": "MCP call middleware; excludes client/network latency. "
                                     "Success does not certify data completeness.",
                "percentile_method": "Histogram bucket upper bounds; rounded milliseconds.",
                "tools": ranked[:limit]}


def create_store(service: str, tools: frozenset[str], env_prefix: str) -> UsageStore:
    root = Path(os.getenv("LOCALAPPDATA") or Path.home())
    return UsageStore(root / service / "tool-usage.sqlite3", tools,
                      enabled=os.getenv(env_prefix + "_MCP_TOOL_USAGE") != "off")


async def observe_call(store, name, context, call_next):
    if not isinstance(name, str) or name not in store.tools:
        return await call_next(context)
    started = perf_counter()
    code = "HANDLER_EXCEPTION"
    try:
        result = await call_next(context)
        try:
            code = result_code(result)
        except Exception:
            code = "UNCLASSIFIED_ERROR"
        return result
    except BaseException as exc:
        with suppress(Exception):
            code = exception_code(exc)
        raise
    finally:
        # Observability must never change a financial or operational result.
        with suppress(Exception):
            store.record(name, (perf_counter()-started)*1000, code)

SERVICE = "avanza-mcp"
ENV_PREFIX = "AVANZA"
KNOWN_TOOLS = frozenset({
    "connect_avanza",
    "connect_forum",
    "create_forum_post",
    "disconnect_avanza",
    "disconnect_forum",
    "download_contract_notes",
    "enrich_option_snapshot",
    "filter_certificates",
    "filter_etfs",
    "filter_warrants",
    "get_accounts",
    "get_active_orders",
    "get_auth_status",
    "get_broker_trade_summary",
    "get_certificate_details",
    "get_certificate_info",
    "get_company_financials",
    "get_deals",
    "get_dividends",
    "get_etf_details",
    "get_etf_info",
    "get_execution_quote",
    "get_forum_auth_status",
    "get_forum_posts",
    "get_fund_chart",
    "get_fund_chart_periods",
    "get_fund_description",
    "get_fund_holdings",
    "get_fund_info",
    "get_fund_sustainability",
    "get_future_forward_details",
    "get_future_forward_filter_options",
    "get_future_forward_info",
    "get_holdings",
    "get_insider_transactions",
    "get_instrument_by_order_book_id",
    "get_instrument_news",
    "get_instrument_news_batch",
    "get_marketmaker_chart",
    "get_marketplace_info",
    "get_number_of_owners",
    "get_orderbook",
    "get_portfolio_insights",
    "get_portfolio_snapshot",
    "get_price_alerts",
    "get_recent_trades",
    "get_short_selling",
    "get_stock_analysis",
    "get_stock_chart",
    "get_stock_info",
    "get_stock_quote",
    "get_stop_loss_orders",
    "get_transactions",
    "get_warrant_details",
    "get_warrant_info",
    "get_watchlists",
    "list_futures_forwards",
    "read_trade_archive",
    "reply_to_forum_post",
    "screen_leveraged_instruments",
    "screen_options",
    "search_instruments",
    "sync_trade_archive",
})

# The FastMCP adapter is intentionally small; use the outer server only for mounted tools.


class ToolUsageMiddleware(Middleware):
    def __init__(self, store: UsageStore):
        self.store = store

    async def on_call_tool(self, context, call_next):
        return await observe_call(self.store, context.message.name, context, call_next)


def install_tool_usage(server):
    store = create_store(SERVICE, KNOWN_TOOLS, ENV_PREFIX)
    atexit.register(store.flush)
    server.add_middleware(ToolUsageMiddleware(store))

    @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False})
    def tool_usage(days: Annotated[int, Field(ge=1, le=30)] = 7,
                   limit: Annotated[int, Field(ge=1, le=100)] = 50,
                   tool: Annotated[str | None, Field(max_length=64)] = None) -> dict:
        """Report local tool calls, errors, latency and registered tools not observed."""
        try:
            return store.report(days, limit, tool)
        except (OSError, sqlite3.Error, ValueError):
            return {"success": False, "error": "Tool usage report unavailable"}

    return store
