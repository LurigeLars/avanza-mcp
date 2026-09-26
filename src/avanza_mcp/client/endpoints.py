"""Avanza API endpoint definitions.

All endpoints are public and require no authentication.
"""

from enum import Enum
import re


class PublicEndpoint(Enum):
    """Public Avanza API endpoints - no authentication required."""

    # Search
    SEARCH = "/_api/search/filtered-search"

    # Market data - Stocks
    STOCK_INFO = "/_api/market-guide/stock/{id}"
    STOCK_ANALYSIS = "/_api/market-guide/stock/{id}/analysis"
    STOCK_QUOTE = "/_api/market-guide/stock/{id}/quote"
    STOCK_MARKETPLACE = "/_api/market-guide/stock/{id}/marketplace"
    STOCK_ORDERDEPTH = "/_api/market-guide/stock/{id}/orderdepth"
    STOCK_TRADES = "/_api/market-guide/stock/{id}/trades"
    STOCK_BROKER_TRADES = "/_api/market-guide/stock/{id}/broker-trade-summaries"
    STOCK_CHART = "/_api/price-chart/stock/{id}"  # Requires timePeriod param

    # Market data - Charts (for traded products: certificates, warrants, ETFs)
    MARKETMAKER_CHART = (
        "/_api/price-chart/marketmaker/{id}"  # Requires timePeriod param
    )

    # Market data - Funds
    FUND_INFO = "/_api/fund-guide/guide/{id}"
    FUND_SUSTAINABILITY = "/_api/fund-reference/sustainability/{id}"
    FUND_CHART = (
        "/_api/fund-guide/chart/{id}/{time_period}"  # time_period: three_years, etc.
    )
    FUND_CHART_PERIODS = "/_api/fund-guide/chart/timeperiods/{id}"
    FUND_DESCRIPTION = "/_api/fund-guide/description/{id}"

    # Market data - Certificates
    CERTIFICATE_FILTER = "/_api/market-certificate-filter/"
    CERTIFICATE_INFO = "/_api/market-guide/certificate/{id}"
    CERTIFICATE_DETAILS = "/_api/market-guide/certificate/{id}/details"

    # Market data - Warrants
    WARRANT_FILTER = "/_api/market-warrant-filter/"
    WARRANT_INFO = "/_api/market-guide/warrant/{id}"
    WARRANT_DETAILS = "/_api/market-guide/warrant/{id}/details"

    # Market data - ETFs
    ETF_FILTER = "/_api/market-etf-filter/"
    ETF_INFO = "/_api/market-etf/{id}"
    ETF_DETAILS = "/_api/market-etf/{id}/details"

    # Market data - Futures/Forwards
    FUTURE_FORWARD_MATRIX = "/_api/market-option-future-forward-list/matrix"
    FUTURE_FORWARD_FILTER_OPTIONS = (
        "/_api/market-option-future-forward-list/filter-options"
    )
    FUTURE_FORWARD_INFO = "/_api/market-guide/futureforward/{id}"
    FUTURE_FORWARD_DETAILS = "/_api/market-guide/futureforward/{id}/details"
    OPTION_INFO = "/_api/market-guide/option/{id}"
    OPTION_DETAILS = "/_api/market-guide/option/{id}/details"

    # Additional features
    NUMBER_OF_OWNERS = "/_api/market-guide/number-of-owners/{id}"
    SHORT_SELLING = "/_api/market-guide/short-selling/{id}"

    def format(self, **kwargs: str | int) -> str:
        """Format endpoint path with variables.

        Args:
            **kwargs: Variables to format into the endpoint path

        Returns:
            Formatted endpoint path
        """
        if "id" in kwargs:
            instrument_id = str(kwargs["id"])
            if not instrument_id.isascii() or not instrument_id.isdecimal():
                raise ValueError("Order-book id must contain only ASCII numeric digits")
        return self.value.format(**kwargs)


_AUTHENTICATED_PUBLIC_EXACT = frozenset(
    {
        ("POST", PublicEndpoint.SEARCH.value),
        ("POST", PublicEndpoint.CERTIFICATE_FILTER.value),
        ("POST", PublicEndpoint.WARRANT_FILTER.value),
        ("POST", PublicEndpoint.ETF_FILTER.value),
        ("POST", PublicEndpoint.FUTURE_FORWARD_MATRIX.value),
        ("GET", PublicEndpoint.FUTURE_FORWARD_FILTER_OPTIONS.value),
    }
)

_AUTHENTICATED_PUBLIC_GET_PATTERNS = (
    re.compile(
        r"^/_api/market-guide/stock/[0-9]+"
        r"(?:/analysis|/quote|/marketplace|/orderdepth|/trades|/broker-trade-summaries)?$"
    ),
    re.compile(r"^/_api/price-chart/stock/[0-9]+$"),
    re.compile(r"^/_api/price-chart/marketmaker/[0-9]+$"),
    re.compile(r"^/_api/fund-guide/guide/[0-9]+$"),
    re.compile(r"^/_api/fund-reference/sustainability/[0-9]+$"),
    re.compile(r"^/_api/fund-guide/chart/[0-9]+/[a-z_]+$"),
    re.compile(r"^/_api/fund-guide/chart/timeperiods/[0-9]+$"),
    re.compile(r"^/_api/fund-guide/description/[0-9]+$"),
    re.compile(r"^/_api/market-guide/certificate/[0-9]+(?:/details)?$"),
    re.compile(r"^/_api/market-guide/warrant/[0-9]+(?:/details)?$"),
    re.compile(r"^/_api/market-etf/[0-9]+(?:/details)?$"),
    re.compile(r"^/_api/market-guide/futureforward/[0-9]+(?:/details)?$"),
    re.compile(r"^/_api/market-guide/option/[0-9]+(?:/details)?$"),
    re.compile(r"^/_api/market-guide/number-of-owners/[0-9]+$"),
    re.compile(r"^/_api/market-guide/short-selling/[0-9]+$"),
)


def authenticated_public_request_allowed(method: str, path: str) -> bool:
    """Return whether an existing public read-only endpoint may reuse login state."""
    normalized_method = method.upper()
    if (normalized_method, path) in _AUTHENTICATED_PUBLIC_EXACT:
        return True
    if normalized_method != "GET":
        return False
    return any(pattern.fullmatch(path) for pattern in _AUTHENTICATED_PUBLIC_GET_PATTERNS)
