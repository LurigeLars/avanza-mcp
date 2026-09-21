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
        description="Maximum candidates to collect per product family.",
    ),
]


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def screen_leveraged_instruments(
    ctx: Context,
    underlying_order_book_id: OrderBookId,
    direction: Literal["long", "short"],
    product_types: list[Literal["certificate", "warrant"]] | None = None,
    max_per_type: MaxPerType = 100,
) -> str:
    """Screen leveraged products for one verified underlying in one bounded call.

    Aggregates certificate and warrant filter pages server-side. Returns compact
    discovery facts such as leverage, spread, bid/ask, turnover, issuer and stop-loss
    when upstream supplies them. Discovery prices are not execution-verified.
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
