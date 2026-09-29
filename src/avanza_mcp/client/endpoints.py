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


class AuthenticatedMarketEndpoint(Enum):
    """Reviewed read-only endpoints that require an authenticated Avanza session."""

    TRADING_CRITICAL_MARKET_DATA = "/_api/trading-critical/rest/marketdata/{id}"
    ORDER_DEPTH_PUSH = "/_push/order-depth-web-push/{id}"

    def format(self, **kwargs: str | int) -> str:
        if "id" in kwargs:
            instrument_id = str(kwargs["id"])
            if not instrument_id.isascii() or not instrument_id.isdecimal():
                raise ValueError("Order-book id must contain only ASCII numeric digits")
        return self.value.format(**kwargs)


_AUTHENTICATED_PUBLIC_EXACT_FAMILIES = {
    ("POST", PublicEndpoint.SEARCH.value): "search",
    ("POST", PublicEndpoint.CERTIFICATE_FILTER.value): "certificate_filter",
    ("POST", PublicEndpoint.WARRANT_FILTER.value): "warrant_filter",
    ("POST", PublicEndpoint.ETF_FILTER.value): "etf_filter",
    ("POST", PublicEndpoint.FUTURE_FORWARD_MATRIX.value): "future_forward_matrix",
    (
        "GET",
        PublicEndpoint.FUTURE_FORWARD_FILTER_OPTIONS.value,
    ): "future_forward_filter_options",
}

_AUTHENTICATED_PUBLIC_GET_FAMILIES = (
    (
        "trading_critical_market_data",
        re.compile(r"^/_api/trading-critical/rest/marketdata/[0-9]+$"),
    ),
    ("stock_info", re.compile(r"^/_api/market-guide/stock/[0-9]+$")),
    ("stock_analysis", re.compile(r"^/_api/market-guide/stock/[0-9]+/analysis$")),
    ("stock_quote", re.compile(r"^/_api/market-guide/stock/[0-9]+/quote$")),
    (
        "stock_marketplace",
        re.compile(r"^/_api/market-guide/stock/[0-9]+/marketplace$"),
    ),
    (
        "stock_orderdepth",
        re.compile(r"^/_api/market-guide/stock/[0-9]+/orderdepth$"),
    ),
    ("stock_trades", re.compile(r"^/_api/market-guide/stock/[0-9]+/trades$")),
    (
        "stock_broker_trades",
        re.compile(r"^/_api/market-guide/stock/[0-9]+/broker-trade-summaries$"),
    ),
    ("stock_chart", re.compile(r"^/_api/price-chart/stock/[0-9]+$")),
    (
        "marketmaker_chart",
        re.compile(r"^/_api/price-chart/marketmaker/[0-9]+$"),
    ),
    ("fund_info", re.compile(r"^/_api/fund-guide/guide/[0-9]+$")),
    (
        "fund_sustainability",
        re.compile(r"^/_api/fund-reference/sustainability/[0-9]+$"),
    ),
    (
        "fund_chart",
        re.compile(r"^/_api/fund-guide/chart/[0-9]+/[a-z_]+$"),
    ),
    (
        "fund_chart_periods",
        re.compile(r"^/_api/fund-guide/chart/timeperiods/[0-9]+$"),
    ),
    (
        "fund_description",
        re.compile(r"^/_api/fund-guide/description/[0-9]+$"),
    ),
    (
        "certificate_info",
        re.compile(r"^/_api/market-guide/certificate/[0-9]+$"),
    ),
    (
        "certificate_details",
        re.compile(r"^/_api/market-guide/certificate/[0-9]+/details$"),
    ),
    ("warrant_info", re.compile(r"^/_api/market-guide/warrant/[0-9]+$")),
    (
        "warrant_details",
        re.compile(r"^/_api/market-guide/warrant/[0-9]+/details$"),
    ),
    ("etf_info", re.compile(r"^/_api/market-etf/[0-9]+$")),
    ("etf_details", re.compile(r"^/_api/market-etf/[0-9]+/details$")),
    (
        "future_forward_info",
        re.compile(r"^/_api/market-guide/futureforward/[0-9]+$"),
    ),
    (
        "future_forward_details",
        re.compile(r"^/_api/market-guide/futureforward/[0-9]+/details$"),
    ),
    ("option_info", re.compile(r"^/_api/market-guide/option/[0-9]+$")),
    (
        "option_details",
        re.compile(r"^/_api/market-guide/option/[0-9]+/details$"),
    ),
    (
        "number_of_owners",
        re.compile(r"^/_api/market-guide/number-of-owners/[0-9]+$"),
    ),
    (
        "short_selling",
        re.compile(r"^/_api/market-guide/short-selling/[0-9]+$"),
    ),
)


def authenticated_public_request_family(method: str, path: str) -> str | None:
    """Return the reviewed read-only endpoint family for auth reuse, if any."""
    normalized_method = method.upper()
    exact = _AUTHENTICATED_PUBLIC_EXACT_FAMILIES.get((normalized_method, path))
    if exact is not None:
        return exact
    if normalized_method != "GET":
        return None
    for family, pattern in _AUTHENTICATED_PUBLIC_GET_FAMILIES:
        if pattern.fullmatch(path):
            return family
    return None


def authenticated_public_request_allowed(method: str, path: str) -> bool:
    """Return whether an existing public read-only endpoint may reuse login state."""
    return authenticated_public_request_family(method, path) is not None
