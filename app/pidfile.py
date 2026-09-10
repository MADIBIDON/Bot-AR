"""Single-instance guard and status reporting for the worker's PID file.

Used by app/main_worker.py (refuse a second instance, clean up a stale
file left by a killed process), app/healthcheck.py, and scripts/watch.py
(`status` command) — one definition of "is a worker already running"
shared by all three, instead of three copies of the same os.kill(pid, 0)
liveness check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PID_FILE = Path("data") / "worker.pid"


class WorkerAlreadyRunningError(Exception):
    """Raised by acquire() when a live process already holds the PID file."""


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    running: bool
    pid: int | None
    stale: bool
    invalid: bool


def get_status(pid_file: Path = DEFAULT_PID_FILE) -> WorkerStatus:
    if not pid_file.exists():
        return WorkerStatus(running=False, pid=None, stale=False, invalid=False)
    pid = _read_pid(pid_file)
    if pid is None:
        return WorkerStatus(running=False, pid=None, stale=False, invalid=True)
    if _is_process_alive(pid):
        return WorkerStatus(running=True, pid=pid, stale=False, invalid=False)
    return WorkerStatus(running=False, pid=pid, stale=True, invalid=False)


def describe_status(status: WorkerStatus) -> str:
    if status.running:
        return f"running (pid={status.pid})"
    if status.invalid:
        return "unknown (invalid pid file)"
    if status.stale:
        return "not running (stale pid file)"
    return "not running"


def acquire(pid_file: Path = DEFAULT_PID_FILE) -> None:
    """Claims the PID file for the current process.

    Refuses with WorkerAlreadyRunningError if a live process already holds
    it. A stale file (the process that wrote it is gone) is silently
    replaced — that is the "detect and clean up" behavior, no manual
    intervention needed for the common case of a worker that was killed
    without going through its normal shutdown path.
    """
    status = get_status(pid_file)
    if status.running:
        raise WorkerAlreadyRunningError(
            f"A worker is already running (pid={status.pid}). Stop it first — "
            "or delete data/worker.pid yourself if you are certain it is not running."
        )
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))


def release(pid_file: Path = DEFAULT_PID_FILE) -> None:
    pid_file.unlink(missing_ok=True)
