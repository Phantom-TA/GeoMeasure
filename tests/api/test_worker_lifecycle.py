"""Workers must not outlive an API process that is killed without a graceful shutdown."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CHILD = """
import json, multiprocessing, os, sys, time
from pathlib import Path
from app.core.config import Settings
from app.db import repository
from app.db.session import get_session_factory, init_db
from app.jobs.runner import JobRunner
from app.services.processing import upload_path

if __name__ == "__main__":
    settings = Settings(data_dir=Path(sys.argv[1]), worker_mode="process", workers=2)
    settings.ensure_dirs()
    init_db(settings.resolved_database_url)
    upload = upload_path(settings, "f" * 32)
    upload.parent.mkdir(parents=True)
    upload.write_bytes(Path("samples/farm_survey.kml").read_bytes())
    with get_session_factory(settings.resolved_database_url)() as session:
        repository.create_file(session, id="f" * 32, filename="farm.kml", size_bytes=1,
                               sha256="x", strategy="auto", crs_override=None)

    runner = JobRunner(settings)
    runner.start()
    runner.submit("f" * 32).result(timeout=60)  # workers have done real work, as in production
    workers = [p.pid for p in multiprocessing.active_children()]
    print(json.dumps({"api": os.getpid(), "workers": workers}), flush=True)
    time.sleep(600)
"""


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_workers_exit_when_api_process_is_killed(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(tmp_path)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline()
        assert line, "child did not start"
        info = json.loads(line)
        assert len(info["workers"]) == 2
        assert all(pid_alive(p) for p in info["workers"])

        # kill the interpreter itself (not a venv launcher), with no chance to clean up
        os.kill(info["api"], getattr(signal, "SIGKILL", signal.SIGTERM))

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(pid_alive(p) for p in info["workers"]):
            time.sleep(0.2)
        assert not any(pid_alive(p) for p in info["workers"]), "orphaned worker processes"
    finally:
        proc.kill()
        proc.wait(timeout=10)
