import pytest


@pytest.fixture(autouse=True)
def _isolated_instrument_catalog(tmp_path, monkeypatch):
    # Without this, tests on a Windows host with a live install read the real catalog in
    # %LOCALAPPDATA%\avanza-mcp and see real Avanza instruments. Tests that need a catalog
    # set their own path, which overrides this one.
    monkeypatch.setenv("AVANZA_MCP_INSTRUMENT_CATALOG", str(tmp_path / "instrument-catalog.sqlite3"))
