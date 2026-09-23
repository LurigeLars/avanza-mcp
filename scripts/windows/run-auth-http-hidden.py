"""Run the authenticated Avanza MCP HTTP server on loopback without a console."""

import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

PORT = int(os.environ.get("AVANZA_MCP_AUTH_PORT", "8768"))
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "avanza-mcp"
LOG_FILE = LOG_DIR / "authenticated-http.log"
OLD_LOG_FILE = LOG_DIR / "authenticated-http.log.1"


def rotate_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size >= 5 * 1024 * 1024:
        if OLD_LOG_FILE.exists():
            OLD_LOG_FILE.unlink()
        LOG_FILE.replace(OLD_LOG_FILE)


def main() -> int:
    rotate_log()
    with LOG_FILE.open("a", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            from avanza_mcp.auth.server import create_auth_server

            create_auth_server().run(
                transport="http",
                host="127.0.0.1",
                port=PORT,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
