import os
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
PORT = os.environ.get("AVANZA_MCP_PORT", "8767")
REPO_ROOT = Path(__file__).resolve().parents[2]
FAST_MCP = REPO_ROOT / ".venv" / "Scripts" / "fastmcp.exe"
LOG_DIR = Path(os.environ["LOCALAPPDATA"]) / "avanza-mcp"
LOG_FILE = LOG_DIR / "public-http.log"
OLD_LOG_FILE = LOG_DIR / "public-http.log.1"

def rotate_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size >= 5 * 1024 * 1024:
        if OLD_LOG_FILE.exists():
            OLD_LOG_FILE.unlink()
        LOG_FILE.replace(OLD_LOG_FILE)

def main() -> int:
    if not FAST_MCP.exists():
        return 2
    rotate_log()
    command = [
        str(FAST_MCP),
        "run",
        "src/avanza_mcp/__init__.py:mcp",
        "--transport",
        "http",
        "--host",
        "127.0.0.1",
        "--port",
        PORT,
    ]
    with LOG_FILE.open("a", encoding="utf-8") as log:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    return completed.returncode

if __name__ == "__main__":
    sys.exit(main())
