"""Refresh the local leveraged-instrument discovery catalog."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from avanza_mcp.instrument_catalog import refresh_instrument_catalog, stats_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        required=True,
        help="Absolute or relative path to the SQLite catalog file.",
    )
    args = parser.parse_args()
    result = asyncio.run(refresh_instrument_catalog(Path(args.db)))
    print(stats_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
