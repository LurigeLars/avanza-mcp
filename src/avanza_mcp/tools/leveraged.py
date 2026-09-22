"""Aggregated leveraged-instrument screen."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastmcp import Context
from pydantic import Field

from .. import mcp
from ..models.common import OrderBookId
from ..services.leveraged_screen_service import LeveragedScreenService
from ._helpers import READ_ONLY, api_errors

MaxPerType = Annotated[
    int,
    Field(
        ge=1,
        le=200,
        description=(
            "Maximum ranked candidates to return per product family after the service "
            "scans the complete matching upstream universe."
        ),
    ),
]


@mcp.tool(annotations=READ_ONLY)
async def screen_leveraged_instruments(
    ctx: Context,
    underlying_order_book_id: OrderBookId,
    direction: Literal["long", "short"],
    product_types: list[Literal["certificate", "warrant"]] | None = None,
    max_per_type: MaxPerType = 100,
):
    """Snapshot and rank leveraged products for one verified underlying.

    The service scans every matching upstream page for the selected product families
    before ranking and applying max_per_type as a return limit. It returns compact
    discovery facts such as leverage, spread, bid/ask, turnover, issuer and stop-loss
    when upstream supplies them, plus snapshot timing and coverage metadata.

    Discovery quotes are collected over a non-atomic time window and are not
    execution-verified.
    """
    selected = product_types or ["certificate", "warrant"]
    with api_errors():
        result = await LeveragedScreenService(
            ctx.lifespan_context["client"]
        ).screen(
            underlying_order_book_id,
            direction,
            selected,
            max_per_type,
        )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
