"""Run one Avanza instrument-catalog refresh without a visible console."""
from __future__ import annotations

import asyncio
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from time import perf_counter

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
            timer = perf_counter()
            print(f"catalog_refresh_started_at={started_at}", flush=True)
            try:
                result = asyncio.run(refresh_instrument_catalog(CATALOG_FILE))
            except Exception as exc:
                failed_at = datetime.now(timezone.utc).isoformat()
                duration_ms = round((perf_counter() - timer) * 1000, 3)
                print(
                    "catalog_refresh_failed_at="
                    f"{failed_at} duration_ms={duration_ms} "
                    f"error_type={type(exc).__name__} error={exc}",
                    flush=True,
                )
                traceback.print_exc()
                return 1

            print(stats_json(result), flush=True)
            completed_at = datetime.now(timezone.utc).isoformat()
            duration_ms = round((perf_counter() - timer) * 1000, 3)
            print(
                f"catalog_refresh_succeeded_at={completed_at} "
                f"duration_ms={duration_ms}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
