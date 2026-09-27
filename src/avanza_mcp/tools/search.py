"""Curated search and exact candidate matching."""

from typing import Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError

from .. import mcp
from ..instrument_catalog import fresh_default_instrument_catalog
from ..models.common import OrderBookId, SearchLimit, SearchQuery
from ..models.search import InstrumentHit, InstrumentSearch
from ..services import SearchService
from ._helpers import READ_ONLY, api_errors


@mcp.tool(annotations=READ_ONLY)
async def search_instruments(
    ctx: Context,
    query: SearchQuery,
    instrument_type: Literal[
        "stock", "fund", "etf", "certificate", "warrant", "all"
    ] = "all",
    limit: SearchLimit = 10,
) -> InstrumentSearch:
    """Search names, tickers or ISINs and return compact instrument identities.

    Select by name, type, exchange, ISIN and currency, not rank alone. Certificate
    and warrant name/ID discovery uses the fresh local daily catalog when available,
    falling back to Avanza search when the catalog is absent, stale, or has no match.
    Other searches examine one upstream page of at most 50 candidates. All excludes
    FAQ, unknown types and invalid
    IDs. Type/ID filtering precedes limit. totalNumberOfHits counts valid matching
    candidates before limit, not a full filtered universe. candidatesExamined
    includes discarded hits; upstreamTotalNumberOfHits is before local filtering.
    No additional pages are fetched. Search is not an authoritative ID registry.
    """
    with api_errors():
        return await SearchService(
            ctx.lifespan_context["client"],
            catalog=fresh_default_instrument_catalog(),
        ).search(query, instrument_type, limit)


@mcp.tool(annotations=READ_ONLY)
async def get_instrument_by_order_book_id(
    ctx: Context, order_book_id: OrderBookId
) -> InstrumentHit:
    """Match an exact order_book_id, preferring the fresh local leveraged catalog.

    Certificate/warrant IDs are resolved exactly from the local daily catalog when
    available. Other IDs fall back to at most 50 Avanza search candidates. Failure
    to match the fallback search does not prove the ID is invalid.
    """
    catalog = fresh_default_instrument_catalog()
    if catalog is not None:
        matches = catalog.find_by_order_book_id(order_book_id)
        if len(matches) == 1:
            row = matches[0]
            return InstrumentHit(
                order_book_id=row["order_book_id"],
                name=row["name"],
                type=str(row["product_type"]).upper(),
                exchange=row.get("marketplace_code"),
            )
        if len(matches) > 1:
            raise ToolError(
                "The local catalog contains multiple product types for this order_book_id; "
                "use search_instruments with an explicit instrument_type."
            )

    with api_errors():
        response = await SearchService(ctx.lifespan_context["client"]).search(
            order_book_id, limit=50
        )
    for hit in response.hits:
        if hit.order_book_id == order_book_id:
            return hit
    raise ToolError(
        f"No exact order_book_id match among {response.candidatesExamined} search candidates "
        "(maximum 50). Search is not authoritative; this does not prove the ID is invalid. "
        "Search by name or ISIN and confirm the returned order_book_id, type and exchange."
    )
