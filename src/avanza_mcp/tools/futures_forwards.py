"""Futures, forwards, and structural option-chain tools."""

import json
from datetime import date
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import mcp
from ..models.common import Limit, Offset, OrderBookId
from ..models.filter import SortBy
from ..models.future_forward import (
    FutureForwardDetails,
    FutureForwardInfo,
    FutureForwardMatrixFilter,
    FutureForwardMatrixRequest,
    FutureForwardMatrixResponse,
)
from ..services import MarketDataService
from ..services.options_screen_service import OptionScreenSpec, OptionsScreenService
from ._helpers import READ_ONLY, api_errors

OptionPageSize = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Rows to return from the frozen option-chain snapshot page. No fixed upper bound; "
            "pagination.total/has_more/next_offset make partial results explicit."
        ),
    ),
]
OptionPageOffset = Annotated[int, Field(ge=0, description="Zero-based option row offset.")]
OptionSnapshotId = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{32}$",
        description="Snapshot ID returned by an earlier screen_options call.",
    ),
]
NonNegativeStrike = Annotated[float, Field(ge=0)]


@mcp.tool(annotations=READ_ONLY)
async def list_futures_forwards(
    ctx: Context,
    underlying_instruments: list[OrderBookId] | None = None,
    option_types: list[str] | None = None,
    call_indicators: list[Literal["CALL", "PUT"]] | None = None,
    end_dates: list[date] | None = None,
    offset: Offset = 0,
    limit: Limit = 20,
    sort_field: str = "strikePrice",
    sort_order: Literal["asc", "desc"] = "desc",
) -> FutureForwardMatrixResponse:
    """Select futures, forwards, or options with server-side pagination.

    Use get_future_forward_filter_options for current option types, CALL/PUT vocabulary,
    underlyings, and ISO YYYY-MM-DD expiry dates. Option rows are returned in matchedOptions;
    the matrix response retains flexible upstream fields rather than inventing a flat schema.
    """
    request = FutureForwardMatrixRequest(
        filter=FutureForwardMatrixFilter(
            underlyingInstruments=underlying_instruments or [],
            optionTypes=option_types or [],
            endDates=[value.isoformat() for value in end_dates or []],
            callIndicators=call_indicators or [],
        ),
        offset=offset,
        limit=limit,
        sortBy=SortBy(field=sort_field, order=sort_order),
    )
    with api_errors():
        return await MarketDataService(
            ctx.lifespan_context["client"]
        ).list_futures_forwards(request)


@mcp.tool(annotations=READ_ONLY)
async def screen_options(
    ctx: Context,
    underlying_order_book_id: OrderBookId,
    option_types: list[str] | None = None,
    call_indicators: list[Literal["CALL", "PUT"]] | None = None,
    end_dates: list[date] | None = None,
    min_strike: NonNegativeStrike | None = None,
    max_strike: NonNegativeStrike | None = None,
    snapshot_id: OptionSnapshotId | None = None,
    offset: OptionPageOffset = 0,
    page_size: OptionPageSize = 100,
):
    """Create or page one complete structural option-chain snapshot.

    Without snapshot_id, discovers current option types/expiries for one verified underlying,
    fully pages every selected type/expiry combination, flattens CALL/PUT contracts, applies
    local CALL/PUT and strike filters, freezes the chain for ten minutes, and returns one page.

    This is structural discovery only: it does not imply live bid/ask, spread, Greeks or
    turnover. Inspect snapshot.market_data_enriched and pagination.has_more. With snapshot_id,
    later pages reuse the same chain and do not refetch Avanza.
    """
    spec = OptionScreenSpec(
        option_types=tuple(option_types or ()),
        call_indicators=tuple(call_indicators or ()),
        end_dates=tuple(value.isoformat() for value in end_dates or ()),
        min_strike=min_strike,
        max_strike=max_strike,
    )
    spec_was_supplied = any(
        (
            option_types,
            call_indicators,
            end_dates,
            min_strike is not None,
            max_strike is not None,
        )
    )
    service = OptionsScreenService(ctx.lifespan_context["client"])
    try:
        if snapshot_id is None:
            if offset != 0:
                raise ValueError("offset must be 0 when creating a new snapshot")
            with api_errors():
                result = await service.screen(
                    underlying_order_book_id,
                    page_size,
                    spec,
                )
        else:
            result = service.get_page(
                snapshot_id,
                underlying_order_book_id,
                offset,
                page_size,
                spec=spec if spec_was_supplied else None,
            )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


@mcp.tool(annotations=READ_ONLY)
async def enrich_option_snapshot(
    ctx: Context,
    snapshot_id: OptionSnapshotId,
    underlying_order_book_id: OrderBookId,
    ranking: Literal["structural", "market_quality"] = "structural",
    offset: OptionPageOffset = 0,
    page_size: OptionPageSize = 20,
):
    """Batch-enrich one page from an existing screen_options snapshot.

    ranking="structural" enriches only the requested structural page and preserves its order.
    ranking="market_quality" enriches the complete structural snapshot once, ranks globally by
    two-way quote, lower midpoint spread and higher turnover, caches that frozen ranking, then
    returns the requested page. Later market-quality pages reuse the cache without refetching.

    There is no fixed page-size upper bound. Quote timestamps and is_real_time are preserved,
    and enrichment is explicitly non-atomic across contracts.
    """
    service = OptionsScreenService(ctx.lifespan_context["client"])
    try:
        with api_errors():
            result = await service.enrich_page(
                snapshot_id,
                underlying_order_book_id,
                offset,
                page_size,
                ranking,
            )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


@mcp.tool(annotations=READ_ONLY)
async def get_future_forward_info(
    ctx: Context, order_book_id: OrderBookId
) -> FutureForwardInfo:
    """Get future, forward, or option identity and latest available market data.\n\n    The service uses the future/forward market-guide path first and falls back to the\n    option-specific path only when Avanza returns not-found. Values are not guaranteed real-time.\n    """
    with api_errors():
        return await MarketDataService(
            ctx.lifespan_context["client"]
        ).get_future_forward_info(order_book_id)


@mcp.tool(annotations=READ_ONLY)
async def get_future_forward_details(
    ctx: Context, order_book_id: OrderBookId
) -> FutureForwardDetails:
    """Get extended future, forward, or option details.\n\n    The service falls back to the option-specific details path only on not-found;\n    detail fields are intentionally flexible.\n    """
    with api_errors():
        return await MarketDataService(
            ctx.lifespan_context["client"]
        ).get_future_forward_details(order_book_id)


@mcp.tool(annotations=READ_ONLY)
async def get_future_forward_filter_options(ctx: Context) -> dict[str, Any]:
    """Get current upstream filter options before selecting futures/forwards/options.

    Option names, values and nested detail fields are intentionally flexible.
    """
    with api_errors():
        return await MarketDataService(
            ctx.lifespan_context["client"]
        ).get_future_forward_filter_options()
