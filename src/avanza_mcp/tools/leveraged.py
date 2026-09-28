"""Snapshot-backed leveraged-instrument screen."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import mcp
from ..models.common import OrderBookId
from ..services.leveraged_screen_service import LeveragedScreenService, ScreenFilters
from ._helpers import READ_ONLY, api_errors

PageSize = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "Rows to return from the ranked snapshot page. No fixed upper bound; "
            "pagination.total/has_more/next_offset make partial results explicit."
        ),
    ),
]
PageOffset = Annotated[int, Field(ge=0, description="Zero-based row offset in the snapshot.")]
SnapshotId = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{32}$",
        description="Snapshot ID returned by an earlier screen_leveraged_instruments call.",
    ),
]
NonNegativeFloat = Annotated[float, Field(ge=0)]
IssuerFilters = Annotated[
    list[str] | None,
    Field(description="Exact issuer names to retain, matched case-insensitively."),
]
SubTypeFilters = Annotated[
    list[str] | None,
    Field(description="Exact product sub-types to retain, matched case-insensitively."),
]
MinLeverage = Annotated[
    NonNegativeFloat | None,
    Field(description="Minimum reported leverage; candidates with unknown leverage are excluded."),
]
MaxLeverage = Annotated[
    NonNegativeFloat | None,
    Field(description="Maximum reported leverage; candidates with unknown leverage are excluded."),
]
MaxSpreadPercent = Annotated[
    NonNegativeFloat | None,
    Field(description="Maximum midpoint spread percent; candidates with unknown spread are excluded."),
]
MinTurnover = Annotated[
    NonNegativeFloat | None,
    Field(description="Minimum reported turnover; candidates with unknown turnover are excluded."),
]



def _screen_filters(
    issuers: list[str] | None,
    sub_types: list[str] | None,
    min_leverage: float | None,
    max_leverage: float | None,
    require_two_way_quote: bool,
    max_spread_percent: float | None,
    min_turnover: float | None,
) -> ScreenFilters:
    return ScreenFilters(
        issuers=tuple(value.strip() for value in issuers or ()),
        sub_types=tuple(value.strip() for value in sub_types or ()),
        min_leverage=min_leverage,
        max_leverage=max_leverage,
        require_two_way_quote=require_two_way_quote,
        max_spread_percent=max_spread_percent,
        min_turnover=min_turnover,
    )


@mcp.tool(annotations=READ_ONLY)
async def screen_leveraged_instruments(
    ctx: Context,
    underlying_order_book_id: OrderBookId,
    direction: Literal["long", "short"],
    product_types: list[Literal["certificate", "warrant"]] | None = None,
    issuers: IssuerFilters = None,
    sub_types: SubTypeFilters = None,
    min_leverage: MinLeverage = None,
    max_leverage: MaxLeverage = None,
    require_two_way_quote: bool = False,
    max_spread_percent: MaxSpreadPercent = None,
    min_turnover: MinTurnover = None,
    snapshot_id: SnapshotId | None = None,
    offset: PageOffset = 0,
    page_size: PageSize = 100,
):
    """Create or page one filtered, ranked leveraged-product snapshot.

    Without snapshot_id, creates one complete ranked result for the requested filters.
    Issuer and warrant sub-type filters may be pushed to Avanza before paging; all supplied
    filters are still re-applied locally before ranking. The frozen result is stored for ten
    minutes and the requested first page is returned.

    The response includes exact issuer/sub-type vocabulary supplemented from upstream filter
    metadata when available. The leverage availability summary describes candidates actually
    scanned after structural pushdown.

    With snapshot_id, returns another page from that same frozen ranking without refetching
    Avanza. Omit product/filter arguments when paging, or repeat semantically equivalent values.
    Always inspect pagination.total, pagination.has_more and pagination.next_offset.

    This tool always runs. It discovers and ranks the full eligible universe from Avanza's
    structural market-data feed. When a valid authenticated session is available, it also refetches
    the requested page through the isolated auth worker and returns execution-ranked bid/ask data.
    Without auth, the same call returns the public discovery page instead of failing. page_size has
    no fixed upper bound. For authenticated leveraged data, freshness is based on bid/ask update age;
    last_trade is informational only.
    """
    selected = product_types or ["certificate", "warrant"]
    selected_filters = _screen_filters(
        issuers,
        sub_types,
        min_leverage,
        max_leverage,
        require_two_way_quote,
        max_spread_percent,
        min_turnover,
    )
    filters_were_supplied = any(
        (
            issuers,
            sub_types,
            min_leverage is not None,
            max_leverage is not None,
            require_two_way_quote,
            max_spread_percent is not None,
            min_turnover is not None,
        )
    )

    service = LeveragedScreenService(ctx.lifespan_context["client"])
    try:
        if snapshot_id is None:
            if offset != 0:
                raise ValueError("offset must be 0 when creating a new snapshot")
            with api_errors():
                result = await service.screen(
                    underlying_order_book_id,
                    direction,
                    selected,
                    page_size,
                    selected_filters,
                )
        else:
            result = service.get_page(
                snapshot_id,
                underlying_order_book_id,
                direction,
                offset,
                page_size,
                product_types=product_types,
                filters=selected_filters if filters_were_supplied else None,
            )

        if result.get("products"):
            with api_errors():
                enriched = await service.enrich_page(
                    result["snapshot_id"],
                    underlying_order_book_id,
                    direction,
                    offset,
                    page_size,
                )

            authenticated_count = int(
                enriched.get("enrichment", {}).get("authenticated_quote_count") or 0
            )
            if authenticated_count > 0:
                discovery_ranking = result.get("ranking")
                result["products"] = enriched.get("products", [])
                result["returned"] = enriched.get("returned", 0)
                result["ranking"] = enriched.get("execution_ranking")
                result["discovery_ranking"] = discovery_ranking
                result["execution"] = {
                    "status": "authenticated",
                    "freshness_basis": enriched.get("execution_freshness_basis"),
                    "last_trade_role": enriched.get("last_trade_role"),
                    "enrichment": enriched.get("enrichment"),
                }
                result["data_note"] = (
                    "The complete eligible universe is discovered structurally before paging. "
                    "The returned page is authenticated and re-ranked from bid/ask quality and "
                    "bid/ask freshness. discovery_bid/discovery_ask are retained only as "
                    "structural provenance. For leveraged market-maker products, last_trade is "
                    "informational only."
                )
            else:
                auth_required = any(
                    product.get("live_market_data_error") == "auth_required"
                    for product in enriched.get("products", [])
                )
                result["execution"] = {
                    "status": "public_fallback",
                    "reason": (
                        "auth_required"
                        if auth_required
                        else "authenticated_market_data_unavailable"
                    ),
                    "data_quality": "discovery_only",
                }
                result["data_note"] = (
                    "No usable authenticated trading-critical quote was available, so this call "
                    "returned the public discovery page instead of failing. discovery_bid and "
                    "discovery_ask may be delayed and are not execution-grade prices."
                )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

