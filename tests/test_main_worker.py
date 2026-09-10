"""No real network, no real Discord, no real worker loop: everything past
the PID guard is monkeypatched so these tests only exercise
app.main_worker's own wiring (single-instance guard, logging setup,
graceful eBay fallback) — the actual monitoring logic is already covered
by tests/test_worker.py and tests/test_app.py.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import app.main_worker as main_worker
from app import pidfile
from market_data.ebay import MissingEbayConfigError


def test_refuses_to_start_when_worker_already_running(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "worker.pid"
    monkeypatch.setattr(main_worker, "PID_FILE", pid_file)
    monkeypatch.setattr(main_worker, "LOG_FILE", tmp_path / "worker.log")

    def raise_running(path: object) -> None:
        raise pidfile.WorkerAlreadyRunningError("A worker is already running (pid=123).")

    monkeypatch.setattr(main_worker.pidfile, "acquire", raise_running)

    called = {"load_dotenv": False}
    monkeypatch.setattr(
        main_worker, "load_dotenv", lambda *a, **k: called.__setitem__("load_dotenv", True)
    )

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(main_worker.main())

    assert excinfo.value.code == 1
    assert called["load_dotenv"] is False  # guard runs before any real setup


def test_stale_pid_file_does_not_block_the_guard(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "worker.pid"
    pid_file.write_text("999999")  # a PID that does not exist

    pidfile.acquire(pid_file)  # must not raise — stale file is cleaned up

    assert pid_file.read_text().strip() != "999999"


def test_build_market_registry_returns_none_without_ebay_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing() -> object:
        raise MissingEbayConfigError("Missing required environment variable(s): EBAY_APP_ID")

    monkeypatch.setattr(main_worker, "build_default_market_registry", raise_missing)

    result = main_worker._build_market_registry()

    assert result is None


def test_configure_logging_creates_log_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = tmp_path / "logs" / "worker.log"
    monkeypatch.setattr(main_worker, "LOG_FILE", log_file)

    main_worker._configure_logging()
    logging.getLogger("test_configure_logging").info("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.exists()
    assert "hello" in log_file.read_text(encoding="utf-8")
