from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "scripts" / "windows"


def test_windows_launcher_scripts_compile() -> None:
    for path in sorted(WINDOWS.glob("*.py")):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_http_launcher_runs_authenticated_fastmcp_in_process() -> None:
    source = (WINDOWS / "run-public-http-hidden.py").read_text(encoding="utf-8")
    assert "from avanza_mcp.auth.server import create_auth_server" in source
    assert "server = create_auth_server()" in source
    assert 'server.run(transport="http", host="127.0.0.1", port=PORT)' in source
    assert "subprocess.run" not in source
    assert "fastmcp.exe" not in source


def test_local_gateway_uses_dedicated_port_auth_surface_and_kill_on_close_job() -> None:
    source = (WINDOWS / "run-local-gateway-hidden.py").read_text(encoding="utf-8")
    helper = (WINDOWS / "_job_process.py").read_text(encoding="utf-8")
    installer = (WINDOWS / "install-local-gateway-task.ps1").read_text(encoding="utf-8")

    assert '"PORT": "8769"' in source
    assert '"ALLOWED_TOOLS": "@authenticated"' in source
    assert "run_child(" in source
    assert "find_node_executable()" in source
    assert "sys.argv" not in source
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in helper
    assert "Get-NetTCPConnection -LocalPort 8769" in installer
    assert "$launcherArg = '\"{0}\"' -f $launcher" in installer


def test_catalog_refresh_launcher_logs_terminal_status_and_duration() -> None:
    source = (WINDOWS / "run-instrument-catalog-refresh-hidden.py").read_text(
        encoding="utf-8"
    )

    assert "catalog_refresh_started_at=" in source
    assert "catalog_refresh_succeeded_at=" in source
    assert "catalog_refresh_failed_at=" in source
    assert "duration_ms=" in source
    assert "traceback.print_exc()" in source
    assert "return 1" in source


def test_worktree_helper_protects_runtime_checkout() -> None:
    source = (WINDOWS / "new-worktree.ps1").read_text(encoding="utf-8")

    assert '$Branch -eq "main"' in source
    assert '"worktree", "list", "--porcelain"' in source
    assert '"worktree", "add", "--track", "-b", $Branch' in source
    assert '"worktree", "add", "-b", $Branch' in source
    assert '"origin/main"' in source
    assert "git switch" not in source
    assert "git checkout" not in source
    assert "git reset" not in source
    assert "git restore" not in source
