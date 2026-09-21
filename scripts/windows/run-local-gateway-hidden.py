"""Run the model-optimized local MCP gateway without a console window."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "public" / "gateway" / "gateway.mjs"
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "avanza-mcp"
LOG_FILE = LOG_DIR / "local-gateway.log"
OLD_LOG_FILE = LOG_DIR / "local-gateway.log.1"


def rotate_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size >= 5 * 1024 * 1024:
        if OLD_LOG_FILE.exists():
            OLD_LOG_FILE.unlink()
        LOG_FILE.replace(OLD_LOG_FILE)


def main() -> int:
    node = shutil.which("node.exe") or shutil.which("node")
    if not node or not GATEWAY.exists():
        return 2

    rotate_log()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_MODE": "local",
            "BIND_HOST": "127.0.0.1",
            "PORT": "8766",
            "UPSTREAM_HOST": "127.0.0.1",
            "UPSTREAM_PORT": "8767",
            "UPSTREAM_PATH": "/mcp",
            "UPSTREAM_HOST_HEADER": "localhost",
        }
    )

    with LOG_FILE.open("a", encoding="utf-8") as log:
        completed = subprocess.run(
            [node, str(GATEWAY)],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
