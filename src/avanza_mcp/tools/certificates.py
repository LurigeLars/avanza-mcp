"""Certificate selection and detail tools."""

from typing import Literal

from fastmcp import Context

from .. import mcp
from ..models.certificate import (
    CertificateDetails,
    CertificateFilter,
    CertificateFilterRequest,
    CertificateFilterResponse,
    CertificateInfo,
)
from ..models.common import Limit, Offset, OrderBookId
from ..models.filter import SortBy
from ..services import MarketDataService
from ._helpers import FilterOptionsMode, READ_ONLY, api_errors, shape_filter_options


@mcp.tool(annotations=READ_ONLY)
async def filter_certificates(
    ctx: Context,
    offset: Offset = 0,
    limit: Limit = 20,
    directions: list[str] | None = None,
    leverages: list[float] | None = None,
    issuers: list[str] | None = None,
    categories: list[str] | None = None,
    exposures: list[str] | None = None,
    underlying_instruments: list[OrderBookId] | None = None,
    filter_options_mode: FilterOptionsMode = "compact",
    sort_field: str = "name",
    sort_order: Literal["asc", "desc"] = "asc",
) -> CertificateFilterResponse:
    """Select certificates by upstream filters with server-side pagination.

    Filter labels are upstream-defined, not a fixed vocabulary. By default,
    filterOptions are compacted: small vocabularies remain, underlyingInstruments
    are omitted in favor of search_instruments, and recursive categories are
    reduced to top-level entries. Set filter_options_mode="full" only when the
    complete upstream metadata is explicitly required, or "none" for minimal output.
    Select an orderbookId from results before requesting info or extended details.
    """
    request = CertificateFilterRequest(
        filter=CertificateFilter(
            directions=directions or [],
            leverages=leverages or [],
            issuers=issuers or [],
            categories=categories or [],
            exposures=exposures or [],
            underlyingInstruments=underlying_instruments or [],
        ),
        offset=offset,
        limit=limit,
        sortBy=SortBy(field=sort_field, order=sort_order),
    )
    with api_errors():
        response = await MarketDataService(
            ctx.lifespan_context["client"]
        ).filter_certificates(request)
    return shape_filter_options(response, filter_options_mode)


@mcp.tool(annotations=READ_ONLY)
async def get_certificate_info(
    ctx: Context, order_book_id: OrderBookId
) -> CertificateInfo:
    """Get certificate identity and latest available market data, not guaranteed real-time."""
    with api_errors():
        return await MarketDataService(
            ctx.lifespan_context["client"]
        ).get_certificate_info(order_book_id)


@mcp.tool(annotations=READ_ONLY)
async def get_certificate_details(
    ctx: Context, order_book_id: OrderBookId
) -> CertificateDetails:
    """Get recognized extended certificate details beyond info."""
    with api_errors():
        return await MarketDataService(
            ctx.lifespan_context["client"]
        ).get_certificate_details(order_book_id)
