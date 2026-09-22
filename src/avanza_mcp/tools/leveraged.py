"""Snapshot-backed leveraged-instrument screen."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import mcp
from ..models.common import OrderBookId
from ..services.leveraged_screen_service import LeveragedScreenService
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


@mcp.tool(annotations=READ_ONLY)
async def screen_leveraged_instruments(
    ctx: Context,
    underlying_order_book_id: OrderBookId,
    direction: Literal["long", "short"],
    product_types: list[Literal["certificate", "warrant"]] | None = None,
    snapshot_id: SnapshotId | None = None,
    offset: PageOffset = 0,
    page_size: PageSize = 100,
):
    """Create or page one ranked leveraged-product snapshot.

    Without snapshot_id, scans and ranks the complete matching upstream universe once,
    stores it for ten minutes, and returns the requested first page. With snapshot_id,
    returns another page from that same frozen ranking without refetching Avanza.

    Always inspect pagination.total, pagination.has_more and pagination.next_offset.
    If has_more is true and the task requires exhaustive analysis, call this tool again
    with the same snapshot_id and next_offset. Never infer completeness from the number
    of rows returned. Snapshots expire after ten minutes or server restart.
    """
    selected = product_types or ["certificate", "warrant"]
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
                )
        else:
            result = service.get_page(
                snapshot_id,
                underlying_order_book_id,
                direction,
                offset,
                page_size,
            )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
