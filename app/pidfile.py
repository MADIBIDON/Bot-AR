"""Single-instance guard and status reporting for the worker's PID file.

21/09 outage fix: "is a worker running?" used to mean "does a process
with the PID in this file exist?". After the Mac slept, the worker died
without removing the file, macOS later handed that same PID to an
unrelated system process, and every restart attempt for 17 hours was
refused because PID 2450 was "alive". The guard is now an exclusive
fcntl.flock held for the worker's whole lifetime: the kernel releases it
the instant the process dies, however it dies, so a leftover file or a
recycled PID can never block a restart again. The file still carries the
PID, for status display only.

Used by app/main_worker.py (refuse a second instance, clean up a stale
file left by a killed process), app/healthcheck.py, and scripts/watch.py
(`status` command) — one definition of "is a worker already running"
shared by all three, instead of three copies of the same check.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO

DEFAULT_PID_FILE = Path("data") / "worker.pid"

# Open handles holding the lock, keyed by resolved path. Kept open for the
# process lifetime — closing the handle would release the lock.
_held: dict[Path, IO[str]] = {}


class WorkerAlreadyRunningError(Exception):
    """Raised by acquire() when a live process already holds the PID file."""


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_locked(pid_file: Path) -> bool:
    """True if some process currently holds the lock on this file —
    including the calling process itself (flock conflicts between two
    separate opens even within one process)."""
    if pid_file.resolve() in _held:
        return True
    try:
        handle = pid_file.open("r")
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


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
    if _is_locked(pid_file):
        return WorkerStatus(running=True, pid=pid, stale=False, invalid=pid is None)
    if pid is None:
        return WorkerStatus(running=False, pid=None, stale=False, invalid=True)
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
    """Takes the exclusive lock for the current process and records its
    PID. Refuses with WorkerAlreadyRunningError only if another live
    worker really holds the lock. A leftover file from a worker that died
    — whatever PID it names — is simply taken over."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    handle = pid_file.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        holder = _read_pid(pid_file)
        raise WorkerAlreadyRunningError(
            f"A worker is already running (pid={holder}). Stop it first."
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _held[pid_file.resolve()] = handle


def release(pid_file: Path = DEFAULT_PID_FILE) -> None:
    handle = _held.pop(pid_file.resolve(), None)
    pid_file.unlink(missing_ok=True)
    if handle is not None:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
