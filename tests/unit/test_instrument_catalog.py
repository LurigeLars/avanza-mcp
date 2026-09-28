from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from avanza_mcp.client.exceptions import AvanzaRateLimitError
from avanza_mcp.instrument_catalog import (
    CatalogInstrument,
    InstrumentCatalog,
    InstrumentCatalogRefresher,
)


class FakeItem:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **_kwargs):
        return dict(self.data)


def _row(
    order_book_id: str,
    name: str,
    *,
    product_type: str = "warrant",
    underlying_id: str = "4478",
    issuer: str = "Issuer A",
) -> CatalogInstrument:
    return CatalogInstrument(
        product_type=product_type,
        order_book_id=order_book_id,
        name=name,
        direction="long",
        issuer=issuer,
        sub_type="TURBO" if product_type == "warrant" else None,
        country_code="SE",
        marketplace_code="NGM",
        underlying_order_book_id=underlying_id,
        underlying_name="NVIDIA",
        underlying_instrument_type="STOCK",
        underlying_country_code="US",
    )


def test_catalog_replaces_atomically_and_supports_local_discovery(tmp_path: Path) -> None:
    catalog = InstrumentCatalog(tmp_path / "catalog.sqlite3")
    result = catalog.replace_all(
        [
            _row("101", "BULL NVIDIA X5", product_type="certificate"),
            _row("202", "MINI L NVIDIA", issuer="Morgan Stanley"),
            _row("303", "TURBO L AMD", underlying_id="999", issuer="Nordea"),
        ],
        refreshed_at=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
    )

    assert result["row_count"] == 3
    assert result["certificate_count"] == 1
    assert result["warrant_count"] == 2

    matches = catalog.find_by_underlying(
        "4478", direction="long", product_types=["certificate", "warrant"]
    )
    assert {item["order_book_id"] for item in matches} == {"101", "202"}

    search = catalog.search("Morgan NVIDIA")
    assert [item["order_book_id"] for item in search] == ["202"]

    assert catalog.count_by_underlying(
        "4478", direction="long", product_types=["certificate", "warrant"]
    ) == 2
    assert catalog.count_by_underlying(
        "4478", product_types=["certificate"]
    ) == 1

    stats = catalog.stats()
    assert stats["row_count"] == 3
    assert stats["refreshed_at"] == "2026-09-27T12:00:00+00:00"
    assert stats["schema_version"] == 2


def test_search_falls_back_to_tokenized_partial_matching_without_fts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = InstrumentCatalog(tmp_path / "catalog.sqlite3")
    catalog.replace_all(
        [_row("202", "MINI L NVIDIA", issuer="Morgan Stanley")],
        refreshed_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(InstrumentCatalog, "_create_schema", staticmethod(lambda _connection: False))

    assert [item["order_book_id"] for item in catalog.search("Morg NVID")] == ["202"]


def test_catalog_rejects_duplicate_identity_before_mutating_existing_data(tmp_path: Path) -> None:
    catalog = InstrumentCatalog(tmp_path / "catalog.sqlite3")
    catalog.replace_all(
        [_row("101", "EXISTING")],
        refreshed_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="Duplicate catalog instrument"):
        catalog.replace_all(
            [_row("202", "A"), _row("202", "B")],
            refreshed_at=datetime.now(timezone.utc),
        )

    assert [item["order_book_id"] for item in catalog.search("EXISTING")] == ["101"]


class PaginatedMarket:
    def __init__(self) -> None:
        self.certificate_offsets: list[int] = []
        self.warrant_offsets: list[int] = []

    async def filter_certificates(self, request):
        self.certificate_offsets.append(request.offset)
        if request.offset == 0:
            items = [
                FakeItem(
                    orderbookId=str(1000 + index),
                    name=f"CERT {index:03d}",
                    direction="long",
                    issuer="Issuer C",
                    countryCode="SE",
                    marketplaceCode="NGM",
                    buyPrice=10.0,
                    sellPrice=10.2,
                    spread=2.0,
                    totalValueTraded=500_000,
                    leverage=3.5,
                    stopLoss=8.5,
                    underlyingInstrument={
                        "orderbookId": "4478",
                        "name": "NVIDIA",
                        "instrumentType": "STOCK",
                        "countryCode": "US",
                    },
                )
                for index in range(100)
            ]
            return SimpleNamespace(certificates=items, totalNumberOfOrderbooks=101)
        return SimpleNamespace(
            certificates=[
                FakeItem(
                    orderbookId="1999",
                    name="CERT FINAL",
                    direction="long",
                    issuer="Issuer C",
                    countryCode="SE",
                    marketplaceCode="NGM",
                    buyPrice=1.0,
                    sellPrice=1.1,
                    underlyingInstrument={
                        "orderbookId": "4478",
                        "name": "NVIDIA",
                    },
                )
            ],
            totalNumberOfOrderbooks=101,
        )

    async def filter_warrants(self, request):
        self.warrant_offsets.append(request.offset)
        return SimpleNamespace(
            warrants=[
                FakeItem(
                    orderbookId="2001",
                    name="MINI L NVIDIA",
                    direction="long",
                    issuer="Morgan Stanley",
                    subType="MINI_FUTURE",
                    countryCode="SE",
                    buyPrice=5.0,
                    sellPrice=5.1,
                    stopLoss=4.0,
                    leverage=5.2,
                    totalValueTraded=750_000,
                    underlyingInstrument={
                        "orderbookId": "4478",
                        "name": "NVIDIA",
                        "instrumentType": "STOCK",
                        "countryCode": "US",
                    },
                )
            ],
            totalNumberOfOrderbooks=1,
        )


@pytest.mark.asyncio
async def test_refresher_fetches_complete_families_and_persists_screening_structure(
    tmp_path: Path,
) -> None:
    market = PaginatedMarket()
    catalog = InstrumentCatalog(tmp_path / "catalog.sqlite3")

    result = await InstrumentCatalogRefresher(market).refresh(catalog)

    assert market.certificate_offsets == [0, 100]
    assert market.warrant_offsets == [0]
    assert result["row_count"] == 102
    assert result["upstream_certificate_total"] == 101
    assert result["upstream_warrant_total"] == 1

    final = catalog.search("CERT FINAL")
    assert final[0]["order_book_id"] == "1999"
    for stale_field in ("buyPrice", "sellPrice", "spread", "totalValueTraded"):
        assert stale_field not in final[0]
    first_cert = catalog.find_by_order_book_id("1000")[0]
    assert first_cert["leverage"] == 3.5
    assert first_cert["stop_loss"] == 8.5
    warrant = catalog.find_by_order_book_id("2001")[0]
    assert warrant["leverage"] == 5.2
    assert warrant["stop_loss"] == 4.0


class RateLimitedMarket:
    def __init__(self) -> None:
        self.calls = 0

    async def filter_certificates(self, request):
        self.calls += 1
        if self.calls == 1:
            raise AvanzaRateLimitError(retry_after=7)
        return SimpleNamespace(certificates=[], totalNumberOfOrderbooks=0)


@pytest.mark.asyncio
async def test_page_retry_honors_retry_after_without_restarting_full_refresh(
    monkeypatch,
) -> None:
    market = RateLimitedMarket()
    delays: list[int] = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("avanza_mcp.instrument_catalog.asyncio.sleep", fake_sleep)
    response = await InstrumentCatalogRefresher(market)._certificate_page(0)

    assert response.totalNumberOfOrderbooks == 0
    assert market.calls == 2
    assert delays == [7]


class IncompleteMarket:
    async def filter_certificates(self, request):
        if request.offset == 0:
            return SimpleNamespace(
                certificates=[
                    FakeItem(
                        orderbookId="1",
                        name="INCOMPLETE",
                        direction="long",
                        issuer="Issuer",
                    )
                ],
                totalNumberOfOrderbooks=2,
            )
        return SimpleNamespace(certificates=[], totalNumberOfOrderbooks=2)

    async def filter_warrants(self, request):
        return SimpleNamespace(warrants=[], totalNumberOfOrderbooks=0)


@pytest.mark.asyncio
async def test_incomplete_refresh_does_not_replace_existing_catalog(tmp_path: Path) -> None:
    catalog = InstrumentCatalog(tmp_path / "catalog.sqlite3")
    catalog.replace_all(
        [_row("101", "EXISTING")],
        refreshed_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="Incomplete certificate catalog fetch"):
        await InstrumentCatalogRefresher(IncompleteMarket()).refresh(catalog)

    assert [item["order_book_id"] for item in catalog.search("EXISTING")] == ["101"]


def test_windows_catalog_task_is_daily_limited_and_uses_local_appdata() -> None:
    root = Path(__file__).resolve().parents[2]
    windows = root / "scripts" / "windows"
    launcher = (windows / "run-instrument-catalog-refresh-hidden.py").read_text(
        encoding="utf-8"
    )
    installer = (windows / "install-instrument-catalog-task.ps1").read_text(
        encoding="utf-8"
    )

    assert "local_appdata_dir()" in launcher
    assert "instrument-catalog.sqlite3" in launcher
    assert "refresh_instrument_catalog(CATALOG_FILE)" in launcher
    assert "subprocess" not in launcher

    assert "AvanzaMcpInstrumentCatalogRefresh" in installer
    assert "New-ScheduledTaskTrigger -Daily" in installer
    assert "-StartWhenAvailable" in installer
    assert "-RunLevel Limited" in installer
    assert "Start-ScheduledTask -TaskName $TaskName" in installer
