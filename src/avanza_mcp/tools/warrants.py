"""Warrant selection and detail tools."""

from typing import Literal

from fastmcp import Context

from .. import mcp
from ..models.common import Limit, Offset, OrderBookId
from ..models.filter import SortBy
from ..models.warrant import (
    WarrantDetails,
    WarrantFilter,
    WarrantFilterRequest,
    WarrantFilterResponse,
    WarrantInfo,
)
from ..services import MarketDataService
from ._helpers import FilterOptionsMode, READ_ONLY, api_errors, shape_filter_options


@mcp.tool(annotations=READ_ONLY)
async def filter_warrants(
    ctx: Context,
    offset: Offset = 0,
    limit: Limit = 20,
    directions: list[str] | None = None,
    sub_types: list[str] | None = None,
    issuers: list[str] | None = None,
    underlying_instruments: list[OrderBookId] | None = None,
    filter_options_mode: FilterOptionsMode = "compact",
    sort_field: str = "name",
    sort_order: Literal["asc", "desc"] = "asc",
) -> WarrantFilterResponse:
    """Select warrants by upstream filters with server-side pagination.

    Sub-type, direction and issuer vocabulary is upstream-defined. By default,
    filterOptions are compacted: small vocabularies remain, underlyingInstruments
    are omitted in favor of search_instruments, and recursive categories are
    reduced to top-level entries. Set filter_options_mode="full" only when the
    complete upstream metadata is explicitly required, or "none" for minimal output.
    Select an orderbookId from results before requesting info or extended details.
    """
    request = WarrantFilterRequest(
        filter=WarrantFilter(
            directions=directions or [],
            subTypes=sub_types or [],
            issuers=issuers or [],
            underlyingInstruments=underlying_instruments or [],
        ),
        offset=offset,
        limit=limit,
        sortBy=SortBy(field=sort_field, order=sort_order),
    )
    with api_errors():
        response = await MarketDataService(
            ctx.lifespan_context["client"]
        ).filter_warrants(request)
    return shape_filter_options(response, filter_options_mode)


@mcp.tool(annotations=READ_ONLY)
async def get_warrant_info(ctx: Context, order_book_id: OrderBookId) -> WarrantInfo:
    """Get warrant identity and latest available market data, not guaranteed real-time."""
    with api_errors():
        return await MarketDataService(ctx.lifespan_context["client"]).get_warrant_info(
            order_book_id
        )


@mcp.tool(annotations=READ_ONLY)
async def get_warrant_details(
    ctx: Context, order_book_id: OrderBookId
) -> WarrantDetails:
    """Get recognized extended warrant details beyond info."""
    with api_errors():
        return await MarketDataService(
            ctx.lifespan_context["client"]
        ).get_warrant_details(order_book_id)
