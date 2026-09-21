from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "scripts" / "windows"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object smoke test")
def test_job_object_kills_child_when_launcher_dies(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        """
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from _job_process import run_child

pid_file = Path(sys.argv[2])
child_code = (
    "import os,time;"
    f"open({str(pid_file)!r},'w').write(str(os.getpid()));"
    "time.sleep(60)"
)
with open(os.devnull, "w", encoding="utf-8") as log:
    raise SystemExit(
        run_child(
            [sys.executable, "-c", child_code],
            cwd=os.getcwd(),
            env=os.environ,
            log=log,
        )
    )
""".strip(),
        encoding="utf-8",
    )

    parent = subprocess.Popen(
        [sys.executable, str(launcher), str(WINDOWS), str(pid_file)],
        creationflags=0x08000000,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not pid_file.exists():
            time.sleep(0.1)
        assert pid_file.exists(), "child PID was never published"
        child_pid = int(pid_file.read_text(encoding="utf-8"))

        parent.terminate()
        parent.wait(timeout=10)

        deadline = time.time() + 10
        still_running = True
        while time.time() < deadline:
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            still_running = str(child_pid) in output
            if not still_running:
                break
            time.sleep(0.2)

        assert not still_running, f"child PID {child_pid} survived launcher termination"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
