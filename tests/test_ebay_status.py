"""market_data/ebay_status.py is a tiny best-effort persisted record, used
by app.healthcheck to report eBay OAuth health without making its own
network call (Phase 26 audit — see app/healthcheck.py::_check_ebay)."""

from __future__ import annotations

from pathlib import Path

import pytest

from market_data import ebay_status


def test_get_status_with_no_file_yet_is_all_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ebay_status, "_STATUS_FILE", tmp_path / "missing.json")

    status = ebay_status.get_status()

    assert status.last_success_at is None
    assert status.last_failure_at is None
    assert status.last_failure_reason is None


def test_record_success_then_failure_preserves_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ebay_status, "_STATUS_FILE", tmp_path / "status.json")

    ebay_status.record_success()
    ebay_status.record_failure("HTTP 401 obtaining eBay OAuth token")

    status = ebay_status.get_status()
    assert status.last_success_at is not None
    assert status.last_failure_at is not None
    assert status.last_failure_reason == "HTTP 401 obtaining eBay OAuth token"


def test_record_success_after_failure_keeps_the_old_failure_on_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ebay_status, "_STATUS_FILE", tmp_path / "status.json")

    ebay_status.record_failure("HTTP 401 obtaining eBay OAuth token")
    ebay_status.record_success()

    status = ebay_status.get_status()
    assert status.last_success_at is not None
    assert status.last_failure_at is not None  # history kept, not wiped


def test_corrupted_status_file_is_treated_as_unknown_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_file = tmp_path / "status.json"
    status_file.write_text("not valid json{{{", encoding="utf-8")
    monkeypatch.setattr(ebay_status, "_STATUS_FILE", status_file)

    status = ebay_status.get_status()

    assert status.last_success_at is None
    assert status.last_failure_at is None


def test_save_failure_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only/unwritable status path must never break the real eBay
    lookup it's a side-channel for."""
    unwritable_dir = tmp_path / "no-such-parent-that-is-actually-a-file"
    unwritable_dir.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(ebay_status, "_STATUS_FILE", unwritable_dir / "status.json")

    ebay_status.record_success()  # must not raise despite the bad path
