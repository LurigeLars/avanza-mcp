"""Run the model-optimized local MCP gateway without a console window."""

from __future__ import annotations

import os
from pathlib import Path

from _job_process import run_child
from _runtime_paths import find_node_executable, local_appdata_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "public" / "gateway" / "gateway.mjs"
LOG_DIR = local_appdata_dir() / "avanza-mcp"
LOG_FILE = LOG_DIR / "local-gateway.log"
OLD_LOG_FILE = LOG_DIR / "local-gateway.log.1"


def rotate_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size >= 5 * 1024 * 1024:
        if OLD_LOG_FILE.exists():
            OLD_LOG_FILE.unlink()
        LOG_FILE.replace(OLD_LOG_FILE)


def main() -> int:
    if not GATEWAY.exists():
        return 2
    try:
        node = find_node_executable()
    except FileNotFoundError:
        return 2

    rotate_log()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_MODE": "local",
            "BIND_HOST": "127.0.0.1",
            "PORT": "8769",
            "UPSTREAM_HOST": "127.0.0.1",
            "UPSTREAM_PORT": "8767",
            "UPSTREAM_PATH": "/mcp",
            "UPSTREAM_HOST_HEADER": "localhost",
            "ALLOWED_TOOLS": "@authenticated",
        }
    )

    with LOG_FILE.open("a", encoding="utf-8") as log:
        return run_child(
            [str(node), str(GATEWAY)],
            cwd=str(REPO_ROOT),
            env=env,
            log=log,
        )


if __name__ == "__main__":
    raise SystemExit(main())
