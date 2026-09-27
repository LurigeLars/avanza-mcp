"""Bounded instrument search contracts using mixed public-search payloads."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from jsonschema import validate

from avanza_mcp import mcp
from avanza_mcp.client import AvanzaClient
from avanza_mcp.client.exceptions import AvanzaNotFoundError
from avanza_mcp.instrument_catalog import CatalogInstrument, InstrumentCatalog


@pytest.fixture
def upstream(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(AvanzaClient, "__aenter__", AsyncMock(return_value=client))
    monkeypatch.setattr(AvanzaClient, "__aexit__", AsyncMock(return_value=None))
    client.get.side_effect = AvanzaNotFoundError("not found")
    client.post.return_value = {
        "totalNumberOfHits": 687,
        "searchQuery": "Volvo",
        "searchFilter": {"types": []},
        "pagination": {"size": 50, "from": 0},
        "facets": {
            "types": [{"type": "STOCK", "count": 4}, {"type": "FAQ", "count": 2}]
        },
        "hits": [
            {
                "type": "FAQ",
                "title": "Vad ar Avanza Global?",
                "orderBookId": None,
                "price": None,
            },
            {
                "type": "CERTIFICATE",
                "title": "BULL VOLVO X9 SG",
                "orderBookId": "1360525",
                "marketPlaceName": "Nordic MTF",
            },
            {"type": "STOCK", "title": "Missing ID"},
            {
                "type": "STOCK",
                "title": "Volvo B (VOLV B)",
                "orderBookId": "5269",
                "marketPlaceName": "Stockholmsborsen",
                "price": {"currency": "SEK", "last": "0,00"},
            },
            {
                "type": "STOCK",
                "title": "Volvo A (VOLV A)",
                "orderBookId": "5268",
                "currency": "EUR",
                "price": {"currency": "SEK"},
            },
            {
                "type": "FUND",
                "title": "Avanza Global",
                "orderBookId": "878733",
                "marketPlaceName": "Fondmarknaden",
                "price": {"currency": "SEK"},
            },
            {"type": "FAQ", "title": "FAQ with ID", "orderBookId": "123"},
            {"type": "UNKNOWN", "title": "Unknown with ID", "orderBookId": "456"},
            {
                "type": "EXCHANGE_TRADED_FUND",
                "title": "iShares ETF",
                "orderBookId": "789",
            },
            {"type": "ETF", "title": "Another ETF", "orderBookId": "790"},
        ],
    }
    return client


@pytest.mark.parametrize(
    "kind,ids,total,server_types",
    [
        ("stock", ["5269"], 2, ["STOCK"]),
        ("fund", ["878733"], 1, ["FUND"]),
        ("certificate", ["1360525"], 1, []),
        ("etf", ["789"], 2, []),
        ("warrant", [], 0, []),
        ("all", ["1360525"], 6, []),
    ],
)
async def test_filter_before_limit_and_candidate_counts(
    upstream, kind, ids, total, server_types
):
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        result = await client.call_tool(
            "search_instruments",
            {"query": "Volvo", "instrument_type": kind, "limit": 1},
        )
    data = result.structured_content
    validate(data, tools["search_instruments"].outputSchema)
    assert [hit["order_book_id"] for hit in data["hits"]] == ids
    assert data["totalNumberOfHits"] == total
    assert data["candidatesExamined"] == 10
    assert data["upstreamTotalNumberOfHits"] == 687
    assert data["returned"] == len(ids)
    assert data["searchQuery"] == "Volvo"
    upstream.post.assert_awaited_once_with(
        "/_api/search/filtered-search",
        json={
            "query": "Volvo",
            "searchFilter": {"types": server_types},
            "pagination": {"size": 50, "from": 0},
        },
    )


async def test_all_id_validation_and_identity_fields(upstream):
    upstream.post.return_value["hits"].extend(
        {"type": "STOCK", "title": "Invalid ID", "orderBookId": value}
        for value in ("", " ", "../123", "12x", "１２３", 123, " 123 ")
    )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_instruments", {"query": "Volvo", "limit": 50}
        )
    data = result.structured_content
    assert data["returned"] == data["totalNumberOfHits"] == 6
    assert data["candidatesExamined"] == 17
    assert data["hits"][1] == {
        "order_book_id": "5269",
        "name": "Volvo B (VOLV B)",
        "type": "STOCK",
        "exchange": "Stockholmsborsen",
        "isin": None,
        "currency": "SEK",
    }
    assert data["hits"][2]["currency"] == "EUR"


async def test_exact_lookup_never_substitutes_or_accepts_faq(upstream):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_instrument_by_order_book_id", {"order_book_id": "5268"}
        )
        assert result.structured_content["order_book_id"] == "5268"
        for identifier in ("123", "456", "526", "999"):
            with pytest.raises(
                ToolError, match="among 10 search candidates.*Search by name or ISIN"
            ):
                await client.call_tool(
                    "get_instrument_by_order_book_id", {"order_book_id": identifier}
                )
    assert upstream.get.await_count == 5
    assert upstream.post.await_count == 5


async def test_candidate_bound_and_empty_response(upstream):
    upstream.post.return_value["hits"] = [
        {"type": "FAQ", "title": "FAQ"} for _ in range(50)
    ] + [{"type": "STOCK", "title": "Beyond bound", "orderBookId": "123"}]
    async with Client(mcp) as client:
        result = await client.call_tool("search_instruments", {"query": "123"})
        assert result.structured_content["candidatesExamined"] == 50
        assert result.structured_content["totalNumberOfHits"] == 0
        assert result.structured_content["hits"] == []
        with pytest.raises(ToolError, match="among 50 search candidates"):
            await client.call_tool(
                "get_instrument_by_order_book_id", {"order_book_id": "123"}
            )
        upstream.post.return_value.update(hits=[], totalNumberOfHits=0)
        result = await client.call_tool("search_instruments", {"query": "none"})
        assert result.structured_content == {
            "totalNumberOfHits": 0,
            "candidatesExamined": 0,
            "upstreamTotalNumberOfHits": 0,
            "hits": [],
            "searchQuery": "Volvo",
            "returned": 0,
        }


async def test_leveraged_search_prefers_fresh_local_catalog(
    upstream, tmp_path, monkeypatch
):
    path = tmp_path / "instrument-catalog.sqlite3"
    InstrumentCatalog(path).replace_all(
        [
            CatalogInstrument(
                product_type="warrant",
                order_book_id="907742",
                name="MINI L NVIDIA AVA 13",
                direction="long",
                issuer="Morgan Stanley",
                sub_type="MINI_FUTURE",
                country_code="SE",
                marketplace_code="FNSE",
                underlying_order_book_id="4478",
                underlying_name="NVIDIA",
                underlying_instrument_type="STOCK",
                underlying_country_code="US",
            )
        ],
        refreshed_at=datetime.now(timezone.utc),
    )
    monkeypatch.setenv("AVANZA_MCP_INSTRUMENT_CATALOG", str(path))

    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_instruments",
            {"query": "NVIDIA", "instrument_type": "warrant", "limit": 10},
        )

    data = result.structured_content
    assert data["returned"] == 1
    assert data["hits"] == [
        {
            "order_book_id": "907742",
            "name": "MINI L NVIDIA AVA 13",
            "type": "WARRANT",
            "exchange": "FNSE",
            "isin": None,
            "currency": None,
        }
    ]
    upstream.post.assert_not_awaited()


async def test_stale_leveraged_catalog_falls_back_to_avanza_search(
    upstream, tmp_path, monkeypatch
):
    path = tmp_path / "instrument-catalog.sqlite3"
    InstrumentCatalog(path).replace_all(
        [
            CatalogInstrument(
                product_type="certificate",
                order_book_id="999999",
                name="BULL VOLVO STALE",
                direction="long",
                issuer="Issuer",
                country_code="SE",
                marketplace_code="NMTF",
                underlying_order_book_id="5269",
                underlying_name="Volvo B",
                underlying_instrument_type="STOCK",
                underlying_country_code="SE",
            )
        ],
        refreshed_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    monkeypatch.setenv("AVANZA_MCP_INSTRUMENT_CATALOG", str(path))

    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_instruments",
            {"query": "Volvo", "instrument_type": "certificate", "limit": 10},
        )

    data = result.structured_content
    assert [hit["order_book_id"] for hit in data["hits"]] == ["1360525"]
    upstream.post.assert_awaited_once()


async def test_exact_lookup_prefers_fresh_leveraged_catalog(
    upstream, tmp_path, monkeypatch
):
    path = tmp_path / "instrument-catalog.sqlite3"
    InstrumentCatalog(path).replace_all(
        [
            CatalogInstrument(
                product_type="warrant",
                order_book_id="907742",
                name="MINI L NVIDIA AVA 13",
                direction="long",
                issuer="Morgan Stanley",
                sub_type="MINI_FUTURE",
                country_code="SE",
                marketplace_code=None,
                underlying_order_book_id="4478",
                underlying_name="NVIDIA",
                underlying_instrument_type="STOCK",
                underlying_country_code="US",
            )
        ],
        refreshed_at=datetime.now(timezone.utc),
    )
    monkeypatch.setenv("AVANZA_MCP_INSTRUMENT_CATALOG", str(path))

    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_instrument_by_order_book_id",
            {"order_book_id": "907742"},
        )

    assert result.structured_content == {
        "order_book_id": "907742",
        "name": "MINI L NVIDIA AVA 13",
        "type": "WARRANT",
        "exchange": None,
        "isin": None,
        "currency": None,
    }
    upstream.post.assert_not_awaited()


async def test_exact_lookup_uses_generic_market_guide_identity(upstream):
    upstream.get.side_effect = None
    upstream.get.return_value = {
        "orderbookId": "4478",
        "name": "NVIDIA",
        "isin": "US67066G1040",
        "listing": {
            "shortName": "NVDA",
            "tickerSymbol": "NVDA",
            "countryCode": "US",
            "currency": "USD",
            "marketPlaceCode": "XNAS",
            "marketPlaceName": "NASDAQ",
        },
        "quote": {},
        "type": "STOCK",
    }

    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_instrument_by_order_book_id",
            {"order_book_id": "4478"},
        )

    assert result.structured_content == {
        "order_book_id": "4478",
        "name": "NVIDIA",
        "type": "STOCK",
        "exchange": "NASDAQ",
        "isin": "US67066G1040",
        "currency": "USD",
    }
    upstream.get.assert_awaited_once_with("/_api/market-guide/stock/4478")
    upstream.post.assert_not_awaited()
