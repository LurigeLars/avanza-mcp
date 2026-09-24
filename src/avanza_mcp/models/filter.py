"""Shared filter models for list/filter endpoints."""

from pydantic import Field
from typing import Literal

# Compatibility re-export for callers that import MODEL_CONFIG from model modules.
# codeql[py/unused-import]
from .common import MODEL_CONFIG as MODEL_CONFIG, AvanzaModel


class SortBy(AvanzaModel):
    """Sort configuration for filter endpoints."""

    field: str
    order: Literal["asc", "desc"]


class PaginationRequest(AvanzaModel):
    """Pagination parameters for filter endpoints."""

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class UnderlyingInstrument(AvanzaModel):
    """Underlying instrument for derivatives."""

    name: str | None = None
    orderbookId: str | None = None
    instrumentType: str | None = None
    countryCode: str | None = None


class FilterResponse(AvanzaModel):
    """Base response for filter endpoints with pagination."""

    pagination: dict | None = None
    totalNumberOfOrderbooks: int | None = None
