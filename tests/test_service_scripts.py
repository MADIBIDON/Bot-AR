"""Exercises the launchd install/uninstall scripts end-to-end, but never
touches the real launchd or the real ~/Library/LaunchAgents: HOME is
pointed at a temp directory and `launchctl` on PATH is replaced with a
harmless stub that only records how it was invoked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
LABEL = "com.retailassistant.worker"


def _sandboxed_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"

    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text(f'#!/bin/bash\necho "$@" >> "{launchctl_log}"\nexit 0\n')
    fake_launchctl.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env, fake_home, launchctl_log


def test_install_generates_plist_with_correct_paths_and_no_secrets(tmp_path: Path) -> None:
    env, fake_home, launchctl_log = _sandboxed_env(tmp_path)

    result = subprocess.run(
        [str(PROJECT_DIR / "scripts" / "install_worker_service.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr

    plist_path = fake_home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert plist_path.exists()
    content = plist_path.read_text()

    assert str(PROJECT_DIR) in content
    assert str(PROJECT_DIR / ".venv" / "bin" / "python") in content
    assert "app.main_worker" in content
    assert "<key>KeepAlive</key>" in content
    assert "<key>RunAtLoad</key>" in content

    # Never embeds real secrets — the app loads .env itself at runtime.
    assert "EnvironmentVariables" not in content
    assert "DISCORD_BOT_TOKEN" not in content
    assert "EBAY_" not in content

    assert launchctl_log.exists()
    log_content = launchctl_log.read_text()
    assert "load" in log_content


def test_install_is_safe_to_run_twice(tmp_path: Path) -> None:
    env, fake_home, _ = _sandboxed_env(tmp_path)
    script = str(PROJECT_DIR / "scripts" / "install_worker_service.sh")

    first = subprocess.run([script], env=env, capture_output=True, text=True, timeout=30)
    second = subprocess.run([script], env=env, capture_output=True, text=True, timeout=30)

    assert first.returncode == 0
    assert second.returncode == 0
    plist_path = fake_home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert plist_path.exists()


def test_uninstall_removes_plist(tmp_path: Path) -> None:
    env, fake_home, _ = _sandboxed_env(tmp_path)
    subprocess.run(
        [str(PROJECT_DIR / "scripts" / "install_worker_service.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    plist_path = fake_home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert plist_path.exists()

    result = subprocess.run(
        [str(PROJECT_DIR / "scripts" / "uninstall_worker_service.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not plist_path.exists()


def test_uninstall_when_not_installed_does_not_fail(tmp_path: Path) -> None:
    env, _fake_home, _ = _sandboxed_env(tmp_path)

    result = subprocess.run(
        [str(PROJECT_DIR / "scripts" / "uninstall_worker_service.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "not installed" in result.stdout.lower()


def test_worker_status_reports_not_installed_when_absent(tmp_path: Path) -> None:
    env, _fake_home, _ = _sandboxed_env(tmp_path)

    result = subprocess.run(
        [str(PROJECT_DIR / "scripts" / "worker_status.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert "not installed" in result.stdout.lower()
