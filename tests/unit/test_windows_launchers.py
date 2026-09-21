from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "scripts" / "windows"


def test_windows_launcher_scripts_compile() -> None:
    for path in sorted(WINDOWS.glob("*.py")):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_public_http_launcher_runs_fastmcp_in_process() -> None:
    source = (WINDOWS / "run-public-http-hidden.py").read_text(encoding="utf-8")
    assert 'mcp.run(transport="http", host="127.0.0.1", port=PORT)' in source
    assert "subprocess.run" not in source
    assert "fastmcp.exe" not in source


def test_local_gateway_uses_dedicated_port_and_kill_on_close_job() -> None:
    source = (WINDOWS / "run-local-gateway-hidden.py").read_text(encoding="utf-8")
    helper = (WINDOWS / "_job_process.py").read_text(encoding="utf-8")
    installer = (WINDOWS / "install-local-gateway-task.ps1").read_text(encoding="utf-8")

    assert '"PORT": "8769"' in source
    assert "run_child(" in source
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in helper
    assert "Get-NetTCPConnection -LocalPort 8769" in installer
