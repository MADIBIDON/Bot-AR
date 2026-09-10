from __future__ import annotations

import os

from app import pidfile


def test_no_file_is_not_running(tmp_path) -> None:
    status = pidfile.get_status(tmp_path / "worker.pid")

    assert status.running is False
    assert status.stale is False
    assert status.invalid is False
    assert pidfile.describe_status(status) == "not running"


def test_acquire_writes_current_pid(tmp_path) -> None:
    path = tmp_path / "worker.pid"

    pidfile.acquire(path)

    assert path.exists()
    assert int(path.read_text().strip()) == os.getpid()


def test_status_reflects_live_process(tmp_path) -> None:
    path = tmp_path / "worker.pid"
    pidfile.acquire(path)

    status = pidfile.get_status(path)

    assert status.running is True
    assert status.pid == os.getpid()
    assert pidfile.describe_status(status) == f"running (pid={os.getpid()})"


def test_acquire_refuses_when_already_running(tmp_path) -> None:
    path = tmp_path / "worker.pid"
    path.write_text(str(os.getpid()))

    try:
        pidfile.acquire(path)
    except pidfile.WorkerAlreadyRunningError as exc:
        assert str(os.getpid()) in str(exc)
    else:
        raise AssertionError("expected WorkerAlreadyRunningError")


def test_stale_pid_file_is_detected(tmp_path) -> None:
    path = tmp_path / "worker.pid"
    # A PID astronomically unlikely to exist on any real system.
    path.write_text("999999")

    status = pidfile.get_status(path)

    assert status.running is False
    assert status.stale is True
    assert pidfile.describe_status(status) == "not running (stale pid file)"


def test_acquire_cleans_up_stale_pid_file(tmp_path) -> None:
    path = tmp_path / "worker.pid"
    path.write_text("999999")

    pidfile.acquire(path)  # must not raise

    assert int(path.read_text().strip()) == os.getpid()


def test_invalid_pid_file_is_reported(tmp_path) -> None:
    path = tmp_path / "worker.pid"
    path.write_text("not-a-pid")

    status = pidfile.get_status(path)

    assert status.invalid is True
    assert pidfile.describe_status(status) == "unknown (invalid pid file)"


def test_release_removes_file(tmp_path) -> None:
    path = tmp_path / "worker.pid"
    pidfile.acquire(path)

    pidfile.release(path)

    assert not path.exists()


def test_release_on_missing_file_does_not_raise(tmp_path) -> None:
    pidfile.release(tmp_path / "does-not-exist.pid")  # must not raise


def test_acquire_creates_parent_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "worker.pid"

    pidfile.acquire(path)

    assert path.exists()
