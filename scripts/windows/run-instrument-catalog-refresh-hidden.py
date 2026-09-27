"""Run one Avanza instrument-catalog refresh without a visible console."""
from __future__ import annotations

import asyncio
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

from _runtime_paths import local_appdata_dir

from avanza_mcp.instrument_catalog import refresh_instrument_catalog, stats_json

RUNTIME_DIR = local_appdata_dir() / "avanza-mcp"
CATALOG_FILE = RUNTIME_DIR / "instrument-catalog.sqlite3"
LOG_FILE = RUNTIME_DIR / "instrument-catalog-refresh.log"
OLD_LOG_FILE = RUNTIME_DIR / "instrument-catalog-refresh.log.1"


def rotate_log() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size >= 5 * 1024 * 1024:
        if OLD_LOG_FILE.exists():
            OLD_LOG_FILE.unlink()
        LOG_FILE.replace(OLD_LOG_FILE)


def main() -> int:
    rotate_log()
    with LOG_FILE.open("a", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            started_at = datetime.now(timezone.utc).isoformat()
            print(f"catalog_refresh_started_at={started_at}", flush=True)
            result = asyncio.run(refresh_instrument_catalog(CATALOG_FILE))
            print(stats_json(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
